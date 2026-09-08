"""提取编排：获取消息并调用搬运层。

取消息策略：
- 私有频道：发送客户端有访问权时直接读取，否则使用用户登录会话，对 -100 前缀做 ID 变体尝试，
  首次失败预取一次对话列表后重试（结果缓存在客户端生命周期内）
- 公开频道：主 bot（或辅助 bot）优先，用户会话为备选（仅 resolve，
  join_chat 只作为最后兜底，避免用户账号被动加入频道）

状态消息整个提取过程只有一条：由本层创建，传入搬运层复用展示
下载/上传进度，最后原地更新为结果。
"""

import asyncio
import logging
from contextlib import suppress

from pyrogram import enums

from tgforward.comments import discussion
from tgforward.config import BATCH_DELAY
from tgforward.runtime import diagnostics, tasks
from tgforward.runtime.tasks import Task, TaskAlreadyActive, TaskCancelled, TaskCooldown
from tgforward.storage.users import load_user_settings, record_extract_success
from tgforward.telegram import clients
from tgforward.transfers import transfer
from tgforward.transfers.progress import TaskStatus
from tgforward.transfers.results import CommentResult, MessageResult
from tgforward.transfers.transfer import TransferError
from tgforward.ui.dialogue import cancel_keyboard
from tgforward.ui.i18n import tr
from tgforward.ui.keyboards import CommentAction
from tgforward.utils.links import MessageLink

logger = logging.getLogger(__name__)

TASK_BUSY_TEXT = "⚠️ 你已有正在进行的任务，请等待完成或发送 /cancel 取消。"
LOGIN_REQUIRED_TEXT = "⚠️ 提取私有频道内容需要先登录，请使用 /login 完成账号验证。"


def _private_chat_ids(chat: str) -> list[int]:
    """将频道标识转换为客户端可解析的候选 ID。"""
    s = str(chat)
    if s.startswith("-100"):
        return [int(s), int(f"-{s[4:]}")]
    if s.lstrip("-").isdigit():
        n = s.lstrip("-")
        return [int(f"-100{n}"), int(f"-{n}")]
    return []


async def _fetch_public(c, username: str, message_id: int):
    try:
        m = await c.get_messages(username, message_id)
        if m and not m.empty:
            return m
    except Exception as e:
        logger.debug("按 username 取消息失败 @%s: %s", username, e)

    try:
        chat = await c.get_chat(f"@{username}")
        m = await c.get_messages(chat.id, message_id)
        if m and not m.empty:
            return m
    except Exception as e:
        logger.debug("resolve 后取消息失败 @%s: %s", username, e)
        me = getattr(c, "me", None)
        # 最后兜底：仅用户账号可加入频道
        if me and not me.is_bot:
            try:
                await c.join_chat(username)
                m = await c.get_messages(username, message_id)
                if m and not m.empty:
                    return m
            except Exception as e2:
                logger.debug("join 后取消息失败 @%s: %s", username, e2)
    return None


async def _fetch_private(user_id: int, user_client, chat: str, message_id: int):
    ids = _private_chat_ids(chat)
    for attempt in range(2):
        for cid in ids:
            try:
                m = await user_client.get_messages(cid, message_id)
                if m and not m.empty:
                    return m
            except Exception as e:
                logger.debug("取私有消息失败 %s/%s: %s", cid, message_id, e)
        if attempt == 0:
            await clients.ensure_dialogs_cached(user_id, user_client)
    return None


async def fetch_message(
    uploader,
    user_client,
    ref: MessageLink,
    message_id: int | None = None,
    user_id: int | None = None,
):
    """获取目标消息，返回 Pyrogram Message 或 None。"""
    mid = ref.message_id if message_id is None else message_id
    if ref.is_private:
        # 私有来源不等于 bot 无权限；bot 已在群/频道时不必先登录用户账号。
        for cid in _private_chat_ids(ref.chat)[:1]:
            try:
                message = await uploader.get_messages(cid, mid)
                if message and not message.empty:
                    return message
            except Exception as e:
                logger.debug("发送客户端读取私有来源失败 type=%s", type(e).__name__)
        if user_client is None:
            return None
        return await _fetch_private(user_id, user_client, ref.chat, mid)

    candidates = [uploader] + ([user_client] if user_client else [])
    for c in candidates:
        m = await _fetch_public(c, ref.chat, mid)
        if m:
            return m
    return None


def _progress_text(j: int, count: int, success: int, failed: int) -> str:
    # j/success/failed 都是当前链接的局部计数；多链接共享的全局 task.success
    # 只在 /status 累计，不与本链接的分母混用，避免“成功 4/2”与进度回退。
    text = tr("⏳ 正在处理 {0}/{1}，已成功 {2}", min(j, count), count, success)
    if failed:
        text += tr("，失败 {0}", failed)
    return text


async def _comment_metadata(ref: MessageLink, msg, settings, media_group=None):
    """仅返回原帖评论元数据；评论入口属于任务管理消息。"""
    reader = getattr(msg, "_client", None)
    try:
        post_id, count = await discussion.post_info(reader, ref.chat, msg, media_group)
    except Exception as exc:
        logger.warning(
            "评论标记读取失败 source=%s/%s type=%s", ref.chat, msg.id, type(exc).__name__
        )
        post_id, count = msg.id, None
    if count is None:
        if msg.chat.type != enums.ChatType.CHANNEL:
            return None
        # 频道的 Raw 标记缺失只表示未确认，不代表没有评论区。
        # 保留无数量的手动入口，点击后由登录账号的 resolver 验证讨论串。
        return CommentAction(ref.chat, post_id, None)
    return CommentAction(ref.chat, post_id, count)


async def _commit_success(task, ref, msg, outcome):
    if isinstance(outcome, MessageResult):
        if outcome.outcome != "success":
            return
        commit_key = outcome.commit_key
    else:
        commit_key = f"{task.token}:{ref.chat}:{ref.message_id}:{msg.id}"
    try:
        await record_extract_success(
            task.user_id,
            ref.chat,
            msg.id,
            ref.is_private,
            outcome.summary,
            commit_key,
            permit=task.lifecycle_permit,
        )
    except Exception:
        logger.exception("提取结果持久化失败 task=%s", task.token)


async def _extract_comments(message, ref, settings, uploader, task, status, comment_action):
    if not settings.auto_comments or comment_action is None:
        return
    task.comment_result = CommentResult()
    try:
        session = await clients.get_user_client(settings.user_id)
        if session is None and not ref.is_private and clients.premium_started:
            session = clients.premium
        if session is None:
            task.comment_result.last_error = tr("评论提取需要登录账号。")
            return
        await discussion.extract(
            session,
            uploader,
            comment_action.chat_ref,
            comment_action.post_id,
            settings,
            message.chat.id,
            task,
            status,
        )
    except TaskCancelled:
        task.comment_result.stopped = True
        raise
    except TransferError as exc:
        task.comment_result.last_error = str(exc)
    except Exception as exc:
        task.comment_result.last_error = str(exc)
        logger.exception("自动提取评论失败 task=%s", task.token)
    finally:
        if task.active_unit is not None and not any(
            item is task.comment_result for item in task.active_unit.comment_results
        ):
            task.active_unit.comment_results.append(task.comment_result)


async def _resolve_comment_link(ref, user_client):
    if ref.comment_id is None:
        return ref, user_client
    session = user_client
    if session is None and not ref.is_private and clients.premium_started:
        session = clients.premium
    if session is None:
        raise TransferError(tr("提取评论链接需要先在「账号与记录」登录账号。"))
    root = await discussion.resolve_root(session, ref.chat, ref.message_id)
    # comment 是群内 ID；以群为读取和传输来源，不能复制频道原帖代替评论。
    return MessageLink(str(root.chat_id), ref.comment_id, True), session


# ─── 单条提取 ────────────────────────────────────────────────────────────────


async def extract_single(message, ref, task: Task | None = None) -> None:
    uid = message.from_user.id
    own_task = task is None
    if own_task:
        try:
            task = tasks.register(uid, "single", 1)
        except TaskAlreadyActive:
            await message.reply(tr(TASK_BUSY_TEXT))
            return
        except TaskCooldown as e:
            await message.reply(tr("⏳ 操作太频繁，请 {0} 秒后再试。", int(e.remaining) + 1))
            return

    try:
        status = await message.reply(tr("⏳ 正在提取..."), reply_markup=cancel_keyboard(uid))
    except BaseException:
        if own_task:
            tasks.finish(uid, task)
        raise
    unit = task.active_unit or task.extraction_unit((ref.chat, ref.message_id, ref.comment_id), 1)
    task.active_unit = unit
    status = TaskStatus.wrap(status, task)
    unit.status = status
    task.status = status
    try:
        task.reset_media(tr("当前链接"))
        user_client = await clients.get_user_client(uid)
        is_post = ref.comment_id is None
        ref, user_client = await _resolve_comment_link(ref, user_client)
        uploader = await clients.get_upload_bot(uid)
        downloader = user_client or uploader

        msg = await fetch_message(uploader, user_client, ref, user_id=uid)
        if not msg or getattr(msg, "empty", False):
            unit.message((ref.chat, ref.message_id)).fail_source(tr("源消息未能读取或已删除"))
            await status.finish(
                tr(LOGIN_REQUIRED_TEXT)
                if ref.is_private and user_client is None
                else tr("⚠️ 消息获取失败，可能已被删除或频道已限制访问。"),
                "failed",
            )
            return

        downloader = getattr(msg, "_client", None) or downloader
        settings = await load_user_settings(uid)
        source_key = (msg.chat.id, getattr(msg, "media_group_id", None) or msg.id)
        try:
            members = (
                await transfer._fetch_media_group(downloader, msg)
                if getattr(msg, "media_group_id", None)
                else None
            )
        except transfer.SourceResolutionError as exc:
            unit.message(source_key).fail_source(str(exc))
            raise
        comment_action = await _comment_metadata(ref, msg, settings, members) if is_post else None
        outcome = await transfer.transfer_message(
            uploader,
            downloader,
            msg,
            settings,
            user_chat_id=str(message.chat.id),
            source_private=ref.is_private,
            task=task,
            status=status,
            media_group=members,
        )
        status.comment_action = comment_action
        await _commit_success(task, ref, msg, outcome)
        task.advance(success=True)
        await _extract_comments(message, ref, settings, uploader, task, status, comment_action)
        await status.finish(
            f"✅ {outcome.summary}"
            + (
                tr("\n\n💬 评论提取\n") + task.comment_result.summary()
                if task.comment_result is not None
                else ""
            ),
            "success",
        )
    except asyncio.CancelledError:
        with suppress(Exception):
            await asyncio.wait_for(
                status.finish(
                    tr("⚠️ 任务超时，提取已停止。") if task.timed_out else tr("🚫 提取已停止。"),
                    "stopped",
                ),
                timeout=5,
            )
        raise
    except TaskCancelled:
        await status.finish(tr("🚫 已取消。"), "stopped")
    except TransferError as e:
        diagnostics.record_error("single", str(e))
        await status.finish(f"⚠️ {e}", "failed")
    except Exception as e:
        logger.exception("提取出错 user=%s: %s", uid, e)
        diagnostics.record_error("single", str(e))
        await status.finish(tr("⚠️ 出错：{0}", str(e)[:100]), "failed")
    finally:
        if task.status is status:
            task.status = None
        if task.active_unit is unit:
            task.active_unit = None
        if own_task:
            tasks.finish(uid)


# ─── 批量提取 ────────────────────────────────────────────────────────────────


async def extract_range(message, ref, count: int, task: Task | None = None) -> None:
    uid = message.from_user.id
    own_task = task is None
    if own_task:
        try:
            task = tasks.register(uid, "batch", count)
        except TaskAlreadyActive:
            await message.reply(tr(TASK_BUSY_TEXT))
            return
        except TaskCooldown as e:
            await message.reply(tr("⏳ 操作太频繁，请 {0} 秒后再试。", int(e.remaining) + 1))
            return

    try:
        status = await message.reply(
            tr("⏳ 开始批量提取（共 {0} 条）...", count), reply_markup=cancel_keyboard(uid)
        )
    except BaseException:
        if own_task:
            tasks.finish(uid, task)
        raise
    unit = task.active_unit or task.extraction_unit(
        (ref.chat, ref.message_id, ref.comment_id), count
    )
    task.active_unit = unit
    status = TaskStatus.wrap(status, task)
    unit.status = status
    task.status = status
    failed = skipped = success_count = 0
    try:
        user_client = await clients.get_user_client(uid)
        is_post = ref.comment_id is None
        ref, user_client = await _resolve_comment_link(ref, user_client)
        uploader = await clients.get_upload_bot(uid)
        downloader = user_client or uploader
        settings = await load_user_settings(uid)

        # 当前链接的局部计数；全局 task.current/success 由 task.advance() 单调累加，
        # 多链接共享同一 task 时不回退、不错配分母。
        failed = skipped = success_count = 0
        task.reset_media(tr("本批内容"))
        task.media_scanning = True
        prepared, prepared_groups = {}, {}
        for offset in range(count):
            task.check_cancel()
            task.touch(tr("读取媒体清单"))
            item_id = ref.message_id + offset
            item = await fetch_message(uploader, user_client, ref, message_id=item_id, user_id=uid)
            prepared[item_id] = item
            if not item or getattr(item, "empty", False):
                unit.message((ref.chat, item_id)).fail_source(tr("源消息未能读取或已删除"))
                continue
            gid = getattr(item, "media_group_id", None)
            if gid:
                group_key = (item.chat.id, gid)
                if group_key not in prepared_groups:
                    try:
                        prepared_groups[group_key] = await transfer._fetch_media_group(
                            getattr(item, "_client", None) or downloader, item
                        )
                    except transfer.SourceResolutionError as exc:
                        unit.message(group_key).fail_source(str(exc))
                        raise
                members = prepared_groups[group_key]
            else:
                members = [item]
            for member in members:
                task.discover(member, ref.is_private)
        task.media_scanning = False

        # 批量只在全部输入明确指向同一个原帖/相册时提供一个手动入口。
        post_keys = {
            (item.chat.id, getattr(item, "media_group_id", None) or item.id)
            for item in prepared.values()
            if item and not getattr(item, "empty", False)
        }
        single_post = len(post_keys) == 1 and all(
            item and not getattr(item, "empty", False) for item in prepared.values()
        )
        group_results = {}  # 相册成功、失败及未尝试成员的真实结果，而非“见过即成功”。
        last_error = ""
        for j in range(count):
            task.check_cancel()
            task.touch(tr("获取消息"))
            mid = ref.message_id + j
            msg = prepared.get(mid)
            if not msg or getattr(msg, "empty", False):
                if ref.is_private and user_client is None:
                    await status.finish(tr(LOGIN_REQUIRED_TEXT), "failed")
                    return
                skipped += 1
                task.advance()
                continue
            gid = getattr(msg, "media_group_id", None)
            group_key = (msg.chat.id, gid) if gid else None
            members = prepared_groups.get(group_key) or [msg]
            result_key = task.media_key(msg)
            if group_key and group_key in group_results:
                result = group_results[group_key][result_key]
            else:
                complete = False
                try:
                    comment_action = (
                        await _comment_metadata(ref, msg, settings, members if gid else None)
                        if is_post
                        else None
                    )
                    outcome = await transfer.transfer_message(
                        uploader,
                        getattr(msg, "_client", None) or downloader,
                        msg,
                        settings,
                        user_chat_id=str(message.chat.id),
                        source_private=ref.is_private,
                        task=task,
                        status=status,
                        media_group=members if gid else None,
                    )
                    complete = True
                    if single_post:
                        status.comment_action = comment_action
                    await _commit_success(task, ref, msg, outcome)
                    await _extract_comments(
                        message, ref, settings, uploader, task, status, comment_action
                    )
                except TaskCancelled:
                    raise
                except Exception as exc:
                    last_error = str(exc)
                    if complete or all(task.media_key(m) in task.media_sent for m in members):
                        task.comment_result = CommentResult(last_error=last_error)
                    diagnostics.record_error("batch", last_error)
                    logger.warning("第 %s 条处理失败：%s", mid, exc)
                results = transfer.member_results(members, task, complete=complete)
                if group_key:
                    group_results[group_key] = results
                result = results[result_key]
            item_ok = result == "sent"
            task.advance(success=item_ok)
            if result == "sent":
                success_count += 1
            elif result == "failed":
                failed += 1
            else:
                skipped += 1
            with suppress(Exception):
                await status.edit(_progress_text(j + 1, count, success_count, failed))
            if j + 1 < count:
                await task.wait_or_cancel(BATCH_DELAY, tr("批量提取间隔"))

        summary = tr("✅ 批量提取完成！成功 {0}/{1}", success_count, count)
        if skipped:
            summary += tr("\n⏭️ 未发送或已删除 {0} 条。", skipped)
        if failed:
            summary += tr("\n⚠️ 失败 {0} 条，最后错误：{1}", failed, last_error[:80])
        elif last_error:
            summary += tr("\n⚠️ 附属操作未完成：{0}", last_error[:80])
        if unit.comment_results:
            summary += tr("\n\n💬 评论提取\n") + unit.comments_summary()
        await status.finish(
            summary,
            "partial" if failed or success_count < count else "success",
        )
    except asyncio.CancelledError:
        with suppress(Exception):
            await asyncio.wait_for(
                status.finish(
                    tr("⚠️ 任务超时，提取已停止。") if task.timed_out else tr("🚫 提取已停止。"),
                    "stopped",
                ),
                timeout=5,
            )
        raise
    except TaskCancelled:
        done = success_count + failed + skipped
        await status.finish(
            tr("🚫 已取消，进度 {0}/{1}，成功 {2}", done, count, success_count), "stopped"
        )
    except TransferError as e:
        await status.finish(f"⚠️ {e}", "failed")
    except Exception as e:
        logger.exception("批量提取出错 user=%s: %s", uid, e)
        await status.finish(tr("⚠️ 出错：{0}", str(e)[:100]), "failed")
    finally:
        if task.status is status:
            task.status = None
        if task.active_unit is unit:
            task.active_unit = None
        if own_task:
            tasks.finish(uid)
