"""评论区文字与附件提取：读取 raw 评论标记，定位讨论串，分页且按相册去重。"""

import asyncio
import logging
import time
from dataclasses import dataclass

from pyrogram import enums, raw, utils
from pyrogram.errors import (
    MessageIdInvalid,
    MsgIdInvalid,
)
from pyrogram.types import Message

from tgforward.runtime.tasks import TaskCancelled
from tgforward.telegram.wait import retry_flood, task_scope
from tgforward.transfers import transfer

logger = logging.getLogger(__name__)
MAX_REPLIES = 1000


@dataclass
class PostSource:
    input_id: int
    album: list[int]
    peer: object
    marked: list

    @property
    def candidates(self):
        # 先尝试 Raw replies 确认的入口；其余成员只作为 RPC 验证的候选。
        marked_ids = [m.id for m in self.marked]
        return list(dict.fromkeys([*marked_ids, self.input_id, *self.album]))


async def inspect_post(client, chat_ref, source=None, media_group=None, *, message_id=None):
    """菜单评论检测和正式提取共用同一套相册/Raw replies 识别。"""
    if str(chat_ref).lstrip("-").isdigit():
        chat_ref = int(chat_ref)
    if source is None:
        source = await _read_stage(
            lambda: client.get_messages(chat_ref, message_id), "读取评论原帖"
        )
    if not source or getattr(source, "empty", False):
        raise transfer.TransferError("评论原帖已删除，或当前账号无权读取。")
    ids = [source.id]
    if getattr(source, "media_group_id", None):
        group = media_group or await _read_stage(
            lambda: client.get_media_group(chat_ref, source.id), "读取完整相册"
        )
        ids = sorted({source.id, *(m.id for m in group)})
    peer = await _read_stage(lambda: client.resolve_peer(chat_ref), "识别评论来源")
    result = await _read_stage(
        lambda: client.invoke(
            raw.functions.channels.GetMessages(
                channel=raw.types.InputChannel(
                    channel_id=peer.channel_id, access_hash=peer.access_hash
                ),
                id=[raw.types.InputMessageID(id=mid) for mid in ids],
            )
        ),
        "确认相册评论入口",
    )
    kind = getattr(getattr(source, "chat", None), "type", None)
    marked = [
        m
        for m in result.messages
        if m.id in ids
        and getattr(m, "replies", None) is not None
        and (getattr(m.replies, "comments", False) or kind == enums.ChatType.SUPERGROUP)
    ]
    return PostSource(source.id, ids, peer, sorted(marked, key=lambda m: m.id))


async def post_info(client, chat_ref, message, media_group=None):
    kind = getattr(message.chat, "type", None)
    if kind not in (enums.ChatType.CHANNEL, enums.ChatType.SUPERGROUP):
        return message.id, None
    post = await inspect_post(client, chat_ref, message, media_group)
    if post.marked:
        entry = post.marked[0]
        return entry.id, getattr(entry.replies, "replies", 0) or 0
    return message.id, None


async def info(client, chat_ref, message):
    return (await post_info(client, chat_ref, message))[1]


def explain_error(exc, stage):
    kind = type(exc).__name__
    reasons = {
        "ChannelPrivate": (
            "当前登录账号无权访问频道或关联讨论组；请用同一账号打开来源和评论区，确认访问权限。"
        ),
        "UserBannedInChannel": "当前登录账号在关联讨论组中被限制。",
        "ChannelInvalid": "频道或讨论组标识未被当前账号正确解析；这不等同于未加入群组。",
        "PeerIdInvalid": "当前账号尚未正确识别关联讨论组。",
        "MessageIdInvalid": "原帖或讨论串已删除，或没有可读取的评论串。",
        "MsgIdInvalid": "原帖或讨论串已删除，或没有可读取的评论串。",
        "BotMethodInvalid": "读取评论需要登录用户账号，机器人身份不支持该接口。",
        "TimeoutError": "Telegram 请求超时，请稍后重试。",
    }
    reason = reasons.get(kind, "发生程序或接口异常，具体堆栈已写入日志；未将它判定为权限问题。")
    return f"评论提取停在「{stage}」（{kind}）：{reason}"


async def _read_stage(make_call, stage):
    try:
        return await retry_flood(make_call, stage=stage)
    except TaskCancelled:
        raise
    except Exception as exc:
        logger.exception("评论提取 stage=%s type=%s", stage, type(exc).__name__)
        raise transfer.TransferError(explain_error(exc, stage)) from exc


@dataclass(frozen=True)
class ThreadRoot:
    chat_id: int
    message_id: int
    peer: object
    root_ids: frozenset


async def _root_from_result(client, result, expected_group=None):
    chats = {c.id: c for c in result.chats}
    candidates = [
        m
        for m in result.messages
        if isinstance(getattr(m, "peer_id", None), raw.types.PeerChannel)
        and getattr(chats.get(m.peer_id.channel_id), "megagroup", False)
        and (expected_group is None or m.peer_id.channel_id == expected_group)
    ]
    if not candidates:
        return None
    # 接口按 ID 倒序返回；只在已确认的关联讨论组内取末条，绝不取频道原帖。
    chosen = candidates[-1]
    chat_id = utils.get_peer_id(chosen.peer_id)
    group = chats[chosen.peer_id.channel_id]
    access_hash = getattr(group, "access_hash", None)
    peer = (
        raw.types.InputPeerChannel(channel_id=group.id, access_hash=access_hash)
        if access_hash is not None
        else await _read_stage(lambda: client.resolve_peer(chat_id), "识别讨论组")
    )
    return ThreadRoot(
        chat_id,
        chosen.id,
        peer,
        frozenset(
            m.id
            for m in candidates
            if m.peer_id.channel_id == group.id
            and (
                m.id == chosen.id
                or (
                    getattr(chosen, "grouped_id", None) is not None
                    and getattr(m, "grouped_id", None) == chosen.grouped_id
                )
            )
        ),
    )


async def resolve_root(client, chat_ref, message_id):
    post = await inspect_post(client, chat_ref, message_id=message_id)
    expected_group = next(
        (
            m.replies.channel_id
            for m in post.marked
            if getattr(m.replies, "channel_id", None) is not None
        ),
        None,
    )
    attempts = []
    for candidate in post.candidates:
        attempts.append(candidate)
        try:
            result = await retry_flood(
                lambda candidate=candidate: client.invoke(
                    raw.functions.messages.GetDiscussionMessage(peer=post.peer, msg_id=candidate)
                ),
                stage="定位关联讨论组",
            )
        except (MessageIdInvalid, MsgIdInvalid) as exc:
            logger.info(
                "评论入口候选无效 input=%s candidate=%s type=%s",
                message_id,
                candidate,
                type(exc).__name__,
            )
            continue
        except TaskCancelled:
            raise
        except Exception as exc:
            # 权限/网络异常不是 ID 错误，不逐个成员重复撞同一错误。
            logger.exception("评论入口解析失败 input=%s candidate=%s", message_id, candidate)
            raise transfer.TransferError(explain_error(exc, "定位关联讨论组")) from exc
        root = await _root_from_result(client, result, expected_group)
        if root is not None:
            logger.info(
                "comment_resolver input=%s album=%s comment_post=%s discussion_chat=%s root=%s",
                message_id,
                post.album,
                candidate,
                root.chat_id,
                root.message_id,
            )
            return root
    logger.warning(
        "评论入口未确认 input=%s album=%s attempted=%s", message_id, post.album, attempts
    )
    raise transfer.TransferError(
        "Telegram 未确认可读取的评论串，已检查同相册候选入口；帖子可能未开放评论或讨论根已删除。"
    )


async def collect(client, chat_ref, message_id, task=None):
    """频道原帖 → 讨论组根 → 评论；所有读取阶段共享任务心跳。"""
    with task_scope(task):
        return await _collect(client, chat_ref, message_id, task)


async def _collect(client, chat_ref, message_id, task=None):
    root = await resolve_root(client, chat_ref, message_id)
    peer = root.peer
    offset = 0
    seen = set()
    result = []
    while True:
        if task:
            task.check_cancel()
            task.touch("读取评论区")
        page = await _read_stage(
            lambda offset=offset: client.invoke(
                raw.functions.messages.GetReplies(
                    peer=peer,
                    msg_id=root.message_id,
                    offset_id=offset,
                    offset_date=0,
                    add_offset=0,
                    limit=100,
                    max_id=0,
                    min_id=0,
                    hash=0,
                )
            ),
            "读取评论消息",
        )
        messages = list(getattr(page, "messages", None) or [])
        if not messages:
            return sorted(result, key=lambda m: m.id), False
        users = {u.id: u for u in getattr(page, "users", [])}
        chats = {c.id: c for c in getattr(page, "chats", [])}
        new = [m for m in messages if m.id not in seen]
        if not new:
            raise transfer.TransferError("评论分页没有前进，请重新提取；未报告为完整成功。")
        for msg in new:
            seen.add(msg.id)
            if msg.id in root.root_ids or getattr(msg, "action", None) is not None:
                continue
            parsed = await _read_stage(
                lambda msg=msg, users=users, chats=chats: Message._parse(
                    client, msg, users, chats, replies=0
                ),
                "解析评论消息",
            )
            if not parsed or getattr(parsed, "empty", False):
                continue
            if parsed.chat.id != root.chat_id:
                continue
            result.append(parsed)
            # 读取多一条才判定截断，避免恰好上限被误判。
            if len(result) > MAX_REPLIES:
                trimmed = result[:MAX_REPLIES]
                # 不把跨上限的相册切成两半，整组留到用户单独提取。
                gid = getattr(result[MAX_REPLIES], "media_group_id", None)
                if gid:
                    trimmed = [m for m in trimmed if getattr(m, "media_group_id", None) != gid]
                return sorted(trimmed, key=lambda m: m.id), True
        oldest = min(m.id for m in messages)
        if offset and oldest >= offset:
            raise transfer.TransferError("评论分页游标异常，请重试。")
        offset = oldest
        if len(messages) < 100:
            return sorted(result, key=lambda m: m.id), False


async def extract(session, uploader, chat_ref, message_id, settings, user_chat_id, task, status):
    from tgforward.transfers.results import CommentResult, MessageResult

    result = task.comment_result = CommentResult()
    unit = task.active_unit or task.extraction_unit(("comments", chat_ref, message_id))
    unit.comment_results.append(result)
    task.touch("读取评论区")
    task.media_scanning = True
    try:
        await transfer._edit(status, "🔎 正在读取评论区……")
        replies, result.truncated = await collect(session, chat_ref, message_id, task)
    except BaseException as exc:
        result.stopped = isinstance(exc, (TaskCancelled, asyncio.CancelledError))
        result.last_error = str(exc) or type(exc).__name__
        raise
    finally:
        task.media_scanning = False
    await transfer._edit(status, f"已读取 {len(replies)} 条评论，准备提取文字和媒体……")
    result.empty = not replies
    task.media_scope = "本次评论" if task.kind == "comments" else "当前内容（含已读取评论）"
    groups = {}
    for message in replies:
        task.discover(message, not bool(getattr(message.chat, "username", None)))
        gid = getattr(message, "media_group_id", None)
        if gid:
            groups.setdefault((message.chat.id, gid), []).append(message)
    done = set()
    last_update = time.monotonic()
    for message in replies:
        source_key = (message.chat.id, getattr(message, "media_group_id", None) or message.id)
        if source_key in done:
            continue
        done.add(source_key)
        group = groups.get(source_key)
        try:
            task.check_cancel()
            task.touch("提取评论附件与文字")
            sent = await transfer.transfer_message(
                uploader,
                session,
                message,
                settings,
                user_chat_id=str(user_chat_id),
                source_private=not bool(getattr(message.chat, "username", None)),
                task=task,
                status=status,
                media_group=group,
                allow_copy_fallback=True,
            )
            if isinstance(sent, MessageResult):
                result.observe(sent)
        except (TaskCancelled, asyncio.CancelledError):
            result.stopped = True
            raise
        except Exception as exc:
            result.last_error = str(exc)[:200] or type(exc).__name__
            logger.warning("评论提取失败 task=%s type=%s", task.token, type(exc).__name__)
        finally:
            for owner in task.units:
                item = owner._messages.get(source_key)
                if item is not None:
                    result.observe(item)
        if time.monotonic() - last_update >= 2:
            counted = (
                result.success + result.failed + result.partial + result.uncertain + result.skipped
            )
            await transfer._edit(
                status,
                f"已读取 {len(replies)} 条评论；已发送 {result.success} 条，"
                f"失败 {result.failed} 条，"
                f"待发送 {max(0, len(replies) - counted)} 条。",
            )
            last_update = time.monotonic()
    summary = result.summary()
    await transfer._edit(status, summary)
    return summary
