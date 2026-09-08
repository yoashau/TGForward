"""消息转存：公开来源直接复制，私有来源下载后上传。

文本应用规则后按 UTF-16 长度分段；相册保持成员顺序和各自 caption。
超过 2GB 的文件通过 Premium 会话和中转频道发送。
调用方管理状态消息，本模块报告传输结果并传播取消信号。
"""

import asyncio
import logging
import os
import time
from contextlib import nullcontext, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import partial

from pyrogram import enums
from pyrogram.errors import (
    ChannelPrivate,
    ChatForwardsRestricted,
    EntitiesTooLong,
    EntityBoundsInvalid,
    FloodWait,
    PeerIdInvalid,
)
from pyrogram.types import InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo

from tgforward.config import LOG_GROUP, MAX_CONCURRENT_TRANSFERS
from tgforward.runtime import lifecycle
from tgforward.runtime.tasks import Task, TaskCancelled
from tgforward.telegram import clients as clients_registry
from tgforward.telegram.wait import acquire, heartbeat_sleep
from tgforward.transfers import delivery, rpc
from tgforward.transfers.downloads import download_media
from tgforward.transfers.progress import make_progress
from tgforward.transfers.results import (
    DeliveryPart,
    MessageDeliveryState,
    MessageResult,
    SideEffectRole,
)
from tgforward.utils.files import apply_name_rules, media_filename, original_media_name
from tgforward.utils.media import custom_thumb_path, get_video_metadata, screenshot
from tgforward.utils.text import (
    append_text,
    apply_text_rules,
    entity_kwargs,
    message_text,
    split_text,
    truncate_text,
)

logger = logging.getLogger(__name__)
_current_result = ContextVar("message_result", default=None)
_copy_fallback = ContextVar("comment_copy_fallback", default=False)

# 全局并发搬运上限：多用户同时提取时保护内存与带宽
_TRANSFER_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_TRANSFERS)

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
LARGE_FILE_GIB = 2  # 超过 2GB 需要 Premium 通道
LARGE_FILE_BYTES = LARGE_FILE_GIB * 1024**3

VIDEO_EXTENSIONS = {
    ".mp4",
    ".avi",
    ".mkv",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".3gp",
    ".ogv",
}
AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".flac",
    ".aac",
    ".ogg",
    ".wma",
    ".m4a",
    ".opus",
    ".aiff",
    ".ac3",
}


class TransferError(Exception):
    """搬运失败，message 为可直接展示给用户的文本。"""


# 全局发送节奏限制：相邻两次发送发起间隔不小于 50ms，
# 降低触发服务端 flood 限制的概率（发送本身不受锁包裹）
_send_lock = asyncio.Lock()
_last_send_at = 0.0
_MIN_SEND_INTERVAL = 0.05


@dataclass
class TransferDescription:
    """发送内容的展示信息；不包含业务成功判定。"""

    summary: str
    # 相册最后一个成员的消息 ID：批量提取时用于整组跳号，避免逐条重复取消息
    last_message_id: int | None = None
    sent_message: object | None = None


def _media_caption(cap: str | None) -> str | None:
    return truncate_text(cap, CAPTION_LIMIT) if cap else None


def _human_size(num_bytes: float) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} TB"


def _media_kind(message) -> str:
    for attr, kind in (
        ("video", "视频"),
        ("photo", "图片"),
        ("audio", "音频"),
        ("voice", "语音"),
        ("video_note", "视频笔记"),
        ("animation", "动图"),
        ("sticker", "贴纸"),
        ("document", "文件"),
    ):
        if getattr(message, attr, None):
            return kind
    return "媒体"


def _media_size(message) -> int:
    for attr in (
        "video",
        "audio",
        "document",
        "animation",
        "photo",
        "voice",
        "video_note",
        "sticker",
    ):
        media = getattr(message, attr, None)
        if media and getattr(media, "file_size", None):
            return int(media.file_size)
    return 0


def _describe_media(message) -> str:
    parts = [_media_kind(message)]
    name = original_media_name(message)
    if name:
        parts.append(f"`{name}`")
    text = " ".join(parts)
    size = _media_size(message)
    if size:
        text += f"（{_human_size(size)}）"
    return text


def _silent_remove(path: str | None) -> None:
    if path:
        with suppress(OSError):
            os.remove(path)


async def _edit(message, text: str) -> None:
    with suppress(Exception):
        await message.edit(text)


async def _delete_quietly(message) -> None:
    with suppress(Exception):
        await message.delete()


async def _send(
    make_call,
    task: Task | None = None,
    *,
    copying=False,
    role=SideEffectRole.FINAL_DELIVERY,
    state=None,
    parts=(),
    batch=False,
):
    """执行一次 Telegram 发送类调用：FloodWait 分段等待后重试，
    PeerIdInvalid 转为用户可读的 TransferError。

    `make_call` 是可重复调用的零参函数（通常为 functools.partial），
    每次调用返回新的 awaitable。
    """
    global _last_send_at
    active = _current_result.get()
    if state is None and active is not None and role == SideEffectRole.FINAL_DELIVERY:
        state = active.delivery

    while True:
        # 只在锁内预订发送时隙；RPC 和 FloodWait 都在锁外执行。
        async with acquire(_send_lock, task, "等待发送时隙"):
            slot = max(time.monotonic(), _last_send_at + _MIN_SEND_INTERVAL)
            _last_send_at = slot
        await heartbeat_sleep(slot - time.monotonic(), task, "等待发送时隙")
        try:
            # 返回后先由业务层提交已送达状态，不在提交之前抛协作式取消。
            if task is not None:
                lifecycle.authorize_side_effect(task, role)
            result = (
                await rpc.execute(
                    make_call,
                    task,
                    state,
                    parts,
                    batch=batch,
                    retryable=(ChannelPrivate, ChatForwardsRestricted, PeerIdInvalid)
                    if copying and _copy_fallback.get()
                    else (),
                )
                if state is not None and role == SideEffectRole.FINAL_DELIVERY
                else await make_call()
            )
            if result is None or isinstance(result, (list, tuple)) and not result:
                if task:
                    task.check_cancel()
                if not copying:
                    raise TransferError("发送未返回成功结果，未计入已发送。")
            if (
                active is not None
                and active.is_comment
                and task is not None
                and task.comment_result is not None
            ):
                task.comment_result.observe(active)
            if state is not None and any(part in state.uncertain for part in parts):
                raise TransferError(
                    ("公开复制" if copying else "")
                    + "部分内容发送结果无法确认，重新提取可能造成重复。"
                )
            return result
        except FloodWait as exc:
            logger.warning("发送限流 wait=%s; retry=server_rejected", exc.value)
            await heartbeat_sleep(max(0, exc.value) + 1, task, "等待 Telegram 发送限流解除")
        except PeerIdInvalid:
            if copying:
                raise
            raise TransferError(
                "目标聊天不可达：请先向机器人发送 /start，或将机器人拉入目标群组。"
            ) from None


@dataclass(frozen=True)
class Destination:
    chat_id: int
    message_thread_id: int | None = None
    reply_to_message_id: int | None = None

    def kwargs(self):
        return {
            "message_thread_id": self.message_thread_id,
            "reply_to_message_id": self.reply_to_message_id,
        }


def _destination_kwargs(destination):
    # None 表示不回复；整数表示回复 ID；话题由 Destination 独立表达。
    return (
        destination.kwargs()
        if isinstance(destination, Destination)
        else {
            "message_thread_id": None,
            "reply_to_message_id": destination,
        }
    )


def _resolve_target(settings, default_chat_id: str) -> Destination:
    """解析发送目标：设置值为 "-100xxx/话题" 或 "-100xxx"，否则发回用户当前会话。"""
    raw = (settings.chat_id or "").strip()
    if raw:
        if "/" in raw:
            chat, _, topic = raw.partition("/")
            if chat.lstrip("-").isdigit() and topic.isdigit():
                return Destination(int(chat), int(topic))
        elif raw.lstrip("-").isdigit():
            return Destination(int(raw))
        logger.warning("发送目标设置非法：%r，回退为当前会话", raw)
    return Destination(int(default_chat_id))


def _build_caption(message, settings) -> tuple[str | None, str | None]:
    """返回 (原生克隆 caption, 物理搬运 caption)。

    原生克隆 caption 为 None 表示不干预、完整保留原始排版。
    """
    original = message_text(message, "caption")
    processed = apply_text_rules(original, settings.replacements, settings.delete_words)
    user_caption = settings.caption

    if user_caption:
        final = append_text(processed, user_caption)
    elif processed != original:
        final = processed
    else:
        final = None

    physical = final if final is not None else (original or None)
    return final, physical


def _album_captions(group: list, settings) -> tuple[list[str | None], list[str]]:
    """计算相册每个成员的最终 caption。

    返回 (finals, originals)：
    - finals[i] 为 None 表示该成员保留原始 caption（不干预）；
      为字符串则覆盖（含被规则清空为 "" 的情况）
    - 预设文案只附加在最后一个成员上（Telegram 相册的惯常显示位置）
    """
    originals = [message_text(gm, "caption") for gm in group]
    finals: list[str | None] = []
    for idx, original in enumerate(originals):
        processed = apply_text_rules(original, settings.replacements, settings.delete_words)
        is_last = idx == len(group) - 1
        if is_last and settings.caption:
            finals.append(append_text(processed, settings.caption))
        elif processed != original:
            finals.append(processed)
        else:
            finals.append(None)
    return finals, originals


def _copy_source(message):
    return getattr(message.chat, "username", None) or message.chat.id


# ─── 入口 ────────────────────────────────────────────────────────────────────


async def transfer_message(
    uploader,
    downloader,
    message,
    settings,
    user_chat_id,
    *,
    source_private,
    task=None,
    status=None,
    media_group=None,
    allow_copy_fallback=False,
    **kwargs,
):
    source_key = (message.chat.id, getattr(message, "media_group_id", None) or message.id)
    if task is not None:
        lifecycle.authorize_side_effect(task, SideEffectRole.DOWNLOAD)
        unit = task.active_unit or task.extraction_unit(source_key)
        result = unit.message(source_key)
    else:
        result = MessageResult(str(source_key), "standalone")
    if result.delivery.uncertain:
        raise TransferError("发送结果无法确认，重新提取可能造成重复发送。")
    if result.delivery.confirmed_failed:
        result.delivery = MessageDeliveryState()
    result.is_comment = allow_copy_fallback
    token = _current_result.set(result)
    fallback_token = _copy_fallback.set(allow_copy_fallback)
    try:
        if getattr(message, "media_group_id", None) and (
            media_group is None or allow_copy_fallback
        ):
            try:
                media_group = await _fetch_media_group(downloader, message)
            except SourceResolutionError as exc:
                result.fail_source(str(exc))
                raise
        members = sorted(media_group, key=lambda m: m.id) if media_group else [message]
        invalid_group = (
            (
                not media_group
                or len(members) > 10
                or message.id not in {m.id for m in members}
                or len({m.id for m in members}) != len(members)
                or any(
                    m.chat.id != message.chat.id
                    or getattr(m, "media_group_id", None) != message.media_group_id
                    for m in members
                )
            )
            if getattr(message, "media_group_id", None)
            else False
        )
        if invalid_group:
            result.fail_source("相册成员清单无效")
            raise SourceResolutionError(result.source_error)
        if Task.is_media(message):
            result.resolve_source(
                [m.id for m in members], [DeliveryPart(f"media:{m.id}", m.id) for m in members]
            )
        try:
            description = await _transfer_entry(
                uploader,
                downloader,
                message,
                settings,
                user_chat_id,
                source_private=source_private,
                task=task,
                status=status,
                media_group=media_group,
                **kwargs,
            )
        except TransferError as exc:
            if (
                source_private
                or not allow_copy_fallback
                or not isinstance(
                    exc.__cause__, (ChannelPrivate, ChatForwardsRestricted, PeerIdInvalid)
                )
            ):
                raise
            description = await _transfer_entry(
                uploader,
                downloader,
                message,
                settings,
                user_chat_id,
                source_private=True,
                task=task,
                status=status,
                media_group=media_group,
                **kwargs,
            )
        result.summary = description.summary
        result.sent_message = description.sent_message
        result.last_message_id = description.last_message_id
        if result.outcome != "success":
            raise TransferError("消息提取未完成；部分内容可能已经发送，重新提取可能重复。")
        return result
    finally:
        result.delivery.settle()
        if result.is_comment and task is not None and task.comment_result is not None:
            task.comment_result.observe(result)
        _current_result.reset(token)
        _copy_fallback.reset(fallback_token)


async def _transfer_entry(
    uploader,
    downloader,
    message,
    settings,
    user_chat_id,
    *,
    source_private,
    task=None,
    status=None,
    media_group=None,
    **kwargs,
):
    group = media_group
    if getattr(message, "media_group_id", None) and group is None:
        group = await _fetch_media_group(downloader, message)
    if group:
        group = sorted(group, key=lambda member: member.id)
    members = group or [message]
    if task:
        from tgforward.transfers.progress import TaskStatus

        for member in members:
            task.discover(member, source_private)
        if status is not None:
            status = TaskStatus.wrap(status, task)
    key = (
        delivery.key_for(
            uploader,
            message,
            members,
            settings,
            settings.chat_id or str(user_chat_id),
            kwargs.get("reply_to_message_id"),
            source_private=source_private,
        )
        if group
        else None
    )
    with delivery.attempt(key) if key is not None else nullcontext():
        active = _current_result.get()
        if active is not None:
            active.delivery.hydrate_delivered(
                f"media:{m.id}" for m in members if Task.media_key(m) in delivery.sent()
            )
        if task:
            resumed = delivery.sent().intersection(task.media_key(m) for m in members)
            task.media_sent.update(resumed)
            task.media_failed.difference_update(resumed)
        try:
            outcome = await _transfer_message(
                uploader,
                downloader,
                message,
                settings,
                user_chat_id,
                source_private=source_private,
                task=task,
                status=status,
                media_group=group,
                **kwargs,
            )
        except TaskCancelled:
            raise
        except Exception:
            if not group:
                _mark_failed(task, members)
            raise
        _commit_sent(task, members)
        return outcome


def _commit_sent(task, members):
    active = _current_result.get()
    keys = {
        Task.media_key(m)
        for m in members
        if Task.is_media(m)
        and (active is None or f"media:{m.id}" in active.delivery.confirmed_delivered)
    }
    delivery.commit(keys)
    if task:
        task.media_sent.update(keys)
        task.media_failed.difference_update(keys)
        task.touch()


def _mark_failed(task, members):
    if task:
        active = _current_result.get()
        task.media_failed.update(
            Task.media_key(m)
            for m in members
            if Task.is_media(m)
            and Task.media_key(m) not in task.media_sent
            and (active is None or f"media:{m.id}" in active.delivery.confirmed_failed)
        )


def member_results(members, task, *, complete=False):
    """返回源消息键 -> sent/failed/skipped；整段异常且无细分记录时按整段失败。"""
    for unit in reversed(task.units):
        source_key = (
            members[0].chat.id,
            getattr(members[0], "media_group_id", None) or members[0].id,
        )
        result = unit._messages.get(source_key)
        if result is not None:
            projected = result.source_outcomes()
            return {
                Task.media_key(m): {"success": "sent", "failed": "failed"}.get(
                    projected.get(m.id), "skipped"
                )
                for m in members
            }
    keys = [Task.media_key(m) for m in members]
    known = set(keys).intersection(task.media_sent | task.media_failed)
    return {
        key: (
            "sent"
            if complete or key in task.media_sent
            else "failed"
            if key in task.media_failed or not known
            else "skipped"
        )
        for key in keys
    }


async def _transfer_message(
    uploader,
    downloader,
    message,
    settings,
    user_chat_id: str,
    *,
    source_private: bool,
    task: Task | None = None,
    status=None,
    media_group: list | None = None,
    reply_to_message_id: int | None = None,
) -> TransferDescription:
    """搬运单条消息（相册整组），返回结果描述；失败抛出 TransferError。

    `status` 为调用方创建的状态消息，本层复用它展示下载/上传进度。
    成果不附加评论入口；评论按钮仅由任务管理消息构建。
    """
    destination = replace(
        _resolve_target(settings, user_chat_id), reply_to_message_id=reply_to_message_id
    )
    target = destination.chat_id

    # ── 纯文本（含网页预览） ──
    if message.text and (
        not message.media
        or getattr(message, "web_page", None) is not None
        or getattr(message, "web_page_preview", None) is not None
    ):
        text = apply_text_rules(message_text(message), settings.replacements, settings.delete_words)
        if settings.caption:
            text = append_text(text, settings.caption)
        chunks = split_text(text, TEXT_LIMIT)
        result = _current_result.get()
        if result is None:
            result = MessageResult(f"text:{message.chat.id}:{message.id}", "standalone")
        result.resolve_source(
            [message.id], [DeliveryPart(f"text:{i}", message.id) for i in range(len(chunks))]
        )
        first_sent = None
        for i, chunk in enumerate(chunks):
            kwargs = {**destination.kwargs(), **entity_kwargs(chunk)}
            call = partial(uploader.send_message, target, chunk, **kwargs)
            try:
                sent = await _send(call, task, state=result.delivery, parts=[f"text:{i}"])
            except (TransferError, TaskCancelled):
                raise
            except (EntityBoundsInvalid, EntitiesTooLong):
                # 仅明确的实体错误可重发；网络/权限/RPC 未知错误直接传播。
                sent = await _send(
                    partial(
                        uploader.send_message,
                        target,
                        chunk,
                        **{**kwargs, "parse_mode": enums.ParseMode.DISABLED, "entities": []},
                    ),
                    task,
                    state=result.delivery,
                    parts=[f"text:{i}"],
                )
            if i == 0:
                first_sent = sent
        result.summary = "完成。"
        result.sent_message = first_sent
        return result

    # ── 媒体搬运受全局并发上限保护（纯文本不占带宽，不参与限流）──
    if task:
        task.touch("等待传输名额")
    async with acquire(_TRANSFER_SEMAPHORE, task, "等待传输名额"):
        # ── 相册 ──
        if getattr(message, "media_group_id", None):
            return await _transfer_album(
                uploader,
                downloader,
                message,
                settings,
                user_chat_id,
                target,
                destination,
                task,
                status,
                source_private,
                media_group=media_group,
            )

        # ── 固定策略：公开只复制，私有直接下载 ──
        final_cap, physical_cap = _build_caption(message, settings)
        if not source_private:
            if task:
                task.touch("直接复制")
            try:
                copy_kwargs = {
                    "caption": "" if final_cap == "" else _media_caption(final_cap),
                    "parse_mode": enums.ParseMode.DISABLED,
                    **_destination_kwargs(destination),
                }
                if final_cap is not None:
                    copy_kwargs.update(entity_kwargs(_media_caption(final_cap), caption=True))
                copied = await _send(
                    partial(
                        uploader.copy_message,
                        target,
                        _copy_source(message),
                        message.id,
                        **copy_kwargs,
                    ),
                    task,
                    copying=True,
                    parts=[f"media:{message.id}"],
                )
                if copied is None:
                    # Message.copy 对 empty/service 消息只写日志并返回 None，不一定抛异常。
                    raise ValueError("Can't copy empty message")
                _commit_sent(task, [message])
                logger.info(
                    "transfer=copy task=%s source_private=%s",
                    task.token if task else "-",
                    source_private,
                )
                return TransferDescription(f"完成：{_describe_media(message)}", sent_message=copied)
            except (TransferError, TaskCancelled):
                raise
            except Exception as e:
                raise TransferError(
                    f"公开来源直接复制失败（{type(e).__name__}）。请检查机器人是否能访问来源及发送目标；公开来源不下载。"
                ) from e

        if task:
            task.touch("下载")
        outcome = await _transfer_physical(
            uploader,
            downloader,
            message,
            settings,
            user_chat_id,
            target,
            destination,
            physical_cap,
            task,
            status,
        )
        return outcome


# ─── 相册 ────────────────────────────────────────────────────────────────────


class SourceResolutionError(TransferError):
    """源相册范围未确定；这不是最终目标发送失败或结果未知。"""


async def _fetch_media_group(downloader, message) -> list:
    """读取相册清单，验证锚点、成员身份和数量；源失败时禁止单条降级。"""
    try:
        group_id = getattr(message, "media_group_id", None)
        if group_id is None:
            raise ValueError("源消息没有相册标识")
        ids = list(range(max(1, message.id - 9), message.id + 10))
        msgs = await downloader.get_messages(message.chat.id, ids)
        if not isinstance(msgs, (list, tuple)):
            raise ValueError("读取相册未返回消息列表")
        group = [
            x
            for x in msgs
            if x
            and not getattr(x, "empty", False)
            and getattr(x, "media_group_id", None) == group_id
        ]
        member_ids = [x.id for x in group]
        if not group or message.id not in member_ids:
            raise ValueError("相册清单缺少原始消息")
        if len(set(member_ids)) != len(member_ids) or len(group) > 10:
            raise ValueError("相册清单包含重复成员或超出成员上限")
        if any(x.chat.id != message.chat.id or x.id not in ids for x in group):
            raise ValueError("相册成员不属于本次源读取范围")
        return sorted(group, key=lambda x: x.id)
    except TaskCancelled:
        raise
    except Exception as exc:
        logger.warning("读取完整相册失败 type=%s", type(exc).__name__)
        raise SourceResolutionError(f"无法读取完整相册：{exc}") from exc


async def _transfer_album(
    uploader,
    downloader,
    message,
    settings,
    user_chat_id,
    target,
    destination,
    task,
    status,
    source_private,
    media_group=None,
) -> TransferDescription:
    group = (
        media_group if media_group is not None else await _fetch_media_group(downloader, message)
    )
    finals, originals = _album_captions(group, settings)
    summary = f"完成：相册（{len(group)} 项）"

    sent_message = delivery.remember_message()

    active = _current_result.get()
    already_sent = (
        {Task.media_key(m) for m in group if f"media:{m.id}" in active.delivery.confirmed_delivered}
        if active is not None
        else delivery.sent()
    )
    pending = [(i, m) for i, m in enumerate(group) if Task.media_key(m) not in already_sent]

    if not source_private:
        if task:
            task.touch("直接复制相册")
        try:
            if len(pending) == len(group):
                kwargs = {"captions": finals} if any(f is not None for f in finals) else {}
                copied = await _send(
                    partial(
                        uploader.copy_media_group,
                        target,
                        _copy_source(message),
                        message.id,
                        **_destination_kwargs(destination),
                        **kwargs,
                    ),
                    task,
                    copying=True,
                    parts=[f"media:{m.id}" for m in group],
                    batch=True,
                )
                if not copied:
                    raise TransferError("公开相册复制未返回成功结果。")
                sent_message = copied[0]
                _commit_sent(task, group)
            else:
                # 续传仅复制待发送成员，避免整组复制重复发送已成功成员。
                for i, member in pending:
                    try:
                        copied = await _send(
                            partial(
                                uploader.copy_message,
                                target,
                                _copy_source(member),
                                member.id,
                                caption=_media_caption(finals[i]) if finals[i] else finals[i],
                                **(
                                    entity_kwargs(_media_caption(finals[i]), caption=True)
                                    if finals[i] is not None
                                    else {"parse_mode": enums.ParseMode.DISABLED}
                                ),
                                **_destination_kwargs(destination),
                            ),
                            task,
                            copying=True,
                            parts=[f"media:{member.id}"],
                        )
                        if copied is None:
                            raise TransferError("公开媒体复制未返回成功结果。")
                        sent_message = sent_message or copied
                        _commit_sent(task, [member])
                    except TaskCancelled:
                        raise
                    except Exception:
                        _mark_failed(task, [member])
                        raise
        except TaskCancelled:
            raise
        except Exception as exc:
            if len(pending) == len(group):
                _mark_failed(task, group)
            raise TransferError(
                f"公开相册直接复制失败（{type(exc).__name__}）；公开来源不下载。"
            ) from exc
        delivery.remember_message(sent_message)
        return TransferDescription(summary, last_message_id=group[-1].id, sent_message=sent_message)

    oversize = any(_media_size(m) > LARGE_FILE_BYTES for _, m in pending)
    if oversize and not (
        clients_registry.premium is not None and clients_registry.premium_started and LOG_GROUP
    ):
        _mark_failed(task, [m for _, m in pending])
        raise TransferError(
            "相册中包含超过 2GB 的文件：需要配置 Premium 会话（STRING + LOG_GROUP）才能上传。"
        )

    # 保留原始成员顺序：连续普通媒体构成 segment，超大成员单独发送。
    segments, current = [], []
    for i, member in pending:
        if _media_size(member) > LARGE_FILE_BYTES:
            if current:
                segments.append(current)
                current = []
            segments.append([(i, member)])
        else:
            current.append((i, member))
            if len(current) == 10:
                segments.append(current)
                current = []
    if current:
        segments.append(current)

    for segment in segments:
        members = [m for _, m in segment]
        try:
            if len(segment) == 1:
                i, member = segment[0]
                cap = finals[i] if finals[i] is not None else originals[i]
                outcome = await _transfer_physical(
                    uploader,
                    downloader,
                    member,
                    settings,
                    user_chat_id,
                    target,
                    destination,
                    cap,
                    task,
                    status,
                )
                sent_message = sent_message or outcome.sent_message
            else:
                sent = await _send_album_physical(
                    uploader,
                    downloader,
                    members,
                    [finals[i] for i, _ in segment],
                    [originals[i] for i, _ in segment],
                    settings,
                    user_chat_id,
                    target,
                    destination,
                    task,
                    status,
                )
                sent_message = sent_message or (sent[0] if sent else None)
            _commit_sent(task, members)
        except TaskCancelled:
            raise
        except Exception:
            _mark_failed(task, members)
            raise

    delivery.remember_message(sent_message)
    return TransferDescription(
        summary + ("，含大文件" if oversize else ""),
        last_message_id=group[-1].id,
        sent_message=sent_message,
    )


async def _send_album_physical(
    uploader,
    downloader,
    group,
    finals,
    originals,
    settings,
    user_chat_id,
    target,
    destination,
    task,
    status,
) -> None:
    """物理搬运整组相册：逐个下载后按原顺序组合重发，逐条保留 caption。"""
    if len(group) == 1:
        cap = finals[0] if finals[0] is not None else originals[0]
        await _transfer_physical(
            uploader,
            downloader,
            group[0],
            settings,
            user_chat_id,
            target,
            destination,
            cap,
            task,
            status,
        )
        return
    if not 2 <= len(group) <= 10:
        raise TransferError("相册成员数量应为 2–10 项。")
    if status is None:
        if task is not None:
            lifecycle.authorize_side_effect(task, SideEffectRole.DOWNLOAD)
        status = await uploader.send_message(
            int(user_chat_id), f"⬇️ 正在下载相册（共 {len(group)} 项）..."
        )
        owned = True
    else:
        owned = False
    if task:
        from tgforward.transfers.progress import TaskStatus

        status = TaskStatus.wrap(status, task)

    files: list[str] = []
    try:
        media_inputs = []
        for idx, gm in enumerate(group):
            if task is not None:
                task.check_cancel()
            path = await download_media(
                downloader,
                gm,
                task=task,
                status=status,
                file_name=media_filename(gm),
                progress=make_progress(
                    uploader,
                    status.chat.id,
                    status.id,
                    task,
                    label="⬇️ 下载中",
                    status_message=status,
                ),
            )
            if task is not None:
                task.check_cancel()
            if not path:
                raise TransferError(f"相册第 {idx + 1} 项下载失败，已停止，未发送残缺相册。")
            files.append(path)
            if task:
                task.media_downloaded.add(task.media_key(gm))
            if _has_original_name(gm):
                path = apply_name_rules(
                    path,
                    delete_words=settings.delete_words,
                    replacements=settings.replacements,
                    rename_tag=settings.rename_tag,
                )
                files[-1] = path
            thumb = custom_thumb_path(settings.user_id)
            # None 表示该成员无规则改动 → 保留原始 caption
            item_cap = finals[idx] if finals[idx] is not None else originals[idx]
            item_cap = _media_caption(item_cap) or None
            cap_kwargs = entity_kwargs(item_cap, caption=True)
            if gm.photo:
                media_inputs.append(InputMediaPhoto(path, caption=item_cap, **cap_kwargs))
            elif gm.video:
                media_inputs.append(
                    InputMediaVideo(path, caption=item_cap, thumb=thumb, **cap_kwargs)
                )
            elif gm.audio:
                media_inputs.append(
                    InputMediaAudio(path, caption=item_cap, thumb=thumb, **cap_kwargs)
                )
            else:
                media_inputs.append(
                    InputMediaDocument(path, caption=item_cap, thumb=thumb, **cap_kwargs)
                )

        if not media_inputs:
            raise TransferError("相册所有项目均下载失败。")

        if task:
            task.touch("上传相册")
        await _edit(status, f"⬆️ 正在上传相册（共 {len(media_inputs)} 项）...")
        sent = await _send(
            partial(
                uploader.send_media_group,
                target,
                media_inputs,
                **_destination_kwargs(destination),
                progress=make_progress(
                    uploader,
                    status.chat.id,
                    status.id,
                    task,
                    label="⬆️ 上传相册",
                    status_message=status,
                    role=SideEffectRole.FINAL_DELIVERY,
                ),
            ),
            task,
            parts=[f"media:{m.id}" for m in group],
            batch=True,
        )
        _commit_sent(task, group)
        return sent
    except TaskCancelled:
        raise
    except TransferError:
        raise
    except Exception as e:
        raise TransferError(str(e)[:100]) from e
    finally:
        for f in files:
            _silent_remove(f)
        if owned:
            await _delete_quietly(status)


# ─── 物理搬运（单条） ─────────────────────────────────────────────────────────


def _has_original_name(message) -> bool:
    return bool(original_media_name(message))


async def _transfer_physical(
    uploader,
    downloader,
    message,
    settings,
    user_chat_id,
    target,
    destination,
    cap,
    task,
    status=None,
) -> TransferDescription:
    if status is None:
        if task is not None:
            lifecycle.authorize_side_effect(task, SideEffectRole.DOWNLOAD)
        status = await uploader.send_message(int(user_chat_id), "⬇️ 正在下载...")
        owned = True
    else:
        owned = False
    if task:
        from tgforward.transfers.progress import TaskStatus

        status = TaskStatus.wrap(status, task)

    file_path = None
    thumb = None
    try:
        if task is not None:
            task.check_cancel()

        file_path = await download_media(
            downloader,
            message,
            task=task,
            status=status,
            file_name=media_filename(message),
            progress=make_progress(
                uploader, status.chat.id, status.id, task, label="⬇️ 下载中", status_message=status
            ),
        )
        if task is not None:
            task.check_cancel()
        if not file_path:
            raise TransferError("下载失败，消息可能不含可下载的媒体内容。")

        if task:
            task.media_downloaded.add(task.media_key(message))
        if _has_original_name(message):
            await _edit(status, "✏️ 正在重命名...")
            file_path = apply_name_rules(
                file_path,
                delete_words=settings.delete_words,
                replacements=settings.replacements,
                rename_tag=settings.rename_tag,
            )

        size_gib = os.path.getsize(file_path) / 1024**3
        ext = os.path.splitext(file_path)[1].lower()
        is_video = bool(getattr(message, "video", None)) or ext in VIDEO_EXTENSIONS

        metadata = await get_video_metadata(file_path) if is_video else None
        thumb = custom_thumb_path(settings.user_id)
        if is_video and thumb is None:
            await _edit(status, "🎬 正在生成封面...")
            thumb = await screenshot(file_path, (metadata or {}).get("duration"))

        size_text = _human_size(os.path.getsize(file_path))
        name_text = f"`{os.path.basename(file_path)}`"

        if size_gib > LARGE_FILE_GIB:
            sent = await _upload_large(
                uploader,
                message,
                file_path,
                cap,
                metadata,
                thumb,
                target,
                destination,
                status,
                task,
            )
            _commit_sent(task, [message])
            return TransferDescription(
                f"完成（Premium 通道）：{name_text}（{size_text}）", sent_message=sent
            )

        if task:
            task.touch("上传")
        await _edit(status, "⬆️ 正在上传...")
        sent = await _upload_regular(
            uploader,
            message,
            file_path,
            cap,
            metadata,
            thumb,
            target,
            destination,
            status,
            task,
        )
        _commit_sent(task, [message])
        kind = _media_kind(message)
        return TransferDescription(f"完成：{kind} {name_text}（{size_text}）", sent_message=sent)

    except TaskCancelled:
        raise
    except TransferError:
        raise
    except Exception as e:
        logger.error("物理搬运失败：%s", e)
        raise TransferError(str(e)[:100]) from e
    finally:
        _silent_remove(file_path)
        if thumb and thumb != custom_thumb_path(settings.user_id):
            _silent_remove(thumb)  # 只清理临时截图，保留用户自定义封面
        if owned:
            await _delete_quietly(status)


async def _upload_large(
    uploader,
    message,
    file_path,
    cap,
    metadata,
    thumb,
    target,
    destination,
    status,
    task,
) -> None:
    premium = clients_registry.premium
    if premium is None or not clients_registry.premium_started or not LOG_GROUP:
        raise TransferError(
            "文件超过 2GB：需要在服务端配置 Premium 会话（STRING + LOG_GROUP）才能上传。"
        )
    progress = make_progress(
        uploader,
        status.chat.id,
        status.id,
        task,
        label="⬆️ 上传中",
        status_message=status,
        role=SideEffectRole.STAGING_UPLOAD,
    )

    if message.video:
        make_call = partial(
            premium.send_video,
            LOG_GROUP,
            file_path,
            thumb=thumb,
            **(metadata or {}),
            caption=_media_caption(cap),
            **entity_kwargs(_media_caption(cap), caption=True),
            progress=progress,
        )
    elif message.audio:
        make_call = partial(
            premium.send_audio,
            LOG_GROUP,
            file_path,
            caption=_media_caption(cap),
            **entity_kwargs(_media_caption(cap), caption=True),
            progress=progress,
        )
    elif message.photo:
        make_call = partial(
            premium.send_photo,
            LOG_GROUP,
            file_path,
            caption=_media_caption(cap),
            **entity_kwargs(_media_caption(cap), caption=True),
            progress=progress,
        )
    elif message.video_note:
        make_call = partial(premium.send_video_note, LOG_GROUP, file_path, progress=progress)
    elif message.voice:
        make_call = partial(
            premium.send_voice,
            LOG_GROUP,
            file_path,
            caption=_media_caption(cap),
            **entity_kwargs(_media_caption(cap), caption=True),
            progress=progress,
        )
    else:
        make_call = partial(
            premium.send_document,
            LOG_GROUP,
            file_path,
            thumb=thumb,
            caption=_media_caption(cap),
            **entity_kwargs(_media_caption(cap), caption=True),
            progress=progress,
        )

    sent = await _send(make_call, task, role=SideEffectRole.STAGING_UPLOAD)
    copy_kwargs = {**_destination_kwargs(destination), "parse_mode": enums.ParseMode.DISABLED}
    return await _send(
        partial(uploader.copy_message, target, LOG_GROUP, sent.id, **copy_kwargs),
        task,
        parts=[f"media:{message.id}"],
    )


async def _upload_regular(
    uploader,
    message,
    file_path,
    cap,
    metadata,
    thumb,
    target,
    destination,
    status,
    task,
):
    progress = make_progress(
        uploader,
        status.chat.id,
        status.id,
        task,
        label="⬆️ 上传中",
        status_message=status,
        role=SideEffectRole.FINAL_DELIVERY,
    )
    ext = os.path.splitext(file_path)[1].lower()
    media_cap = _media_caption(cap)

    if message.video_note:
        make_call = partial(
            uploader.send_video_note,
            target,
            file_path,
            progress=progress,
            **_destination_kwargs(destination),
        )
    elif message.voice:
        make_call = partial(
            uploader.send_voice,
            target,
            file_path,
            caption=media_cap,
            **entity_kwargs(media_cap, caption=True),
            progress=progress,
            **_destination_kwargs(destination),
        )
    elif message.animation:
        make_call = partial(
            uploader.send_animation,
            target,
            file_path,
            caption=media_cap,
            **entity_kwargs(media_cap, caption=True),
            progress=progress,
            **_destination_kwargs(destination),
        )
    elif message.sticker:
        make_call = partial(
            uploader.send_sticker,
            target,
            file_path,
            **_destination_kwargs(destination),
        )
    elif message.video or ext in VIDEO_EXTENSIONS:
        make_call = partial(
            uploader.send_video,
            target,
            file_path,
            caption=media_cap,
            **entity_kwargs(media_cap, caption=True),
            thumb=thumb,
            **(metadata or {}),
            progress=progress,
            **_destination_kwargs(destination),
        )
    elif message.audio or ext in AUDIO_EXTENSIONS:
        make_call = partial(
            uploader.send_audio,
            target,
            file_path,
            caption=media_cap,
            **entity_kwargs(media_cap, caption=True),
            thumb=thumb,
            progress=progress,
            **_destination_kwargs(destination),
        )
    elif message.photo:
        make_call = partial(
            uploader.send_photo,
            target,
            file_path,
            caption=media_cap,
            **entity_kwargs(media_cap, caption=True),
            progress=progress,
            **_destination_kwargs(destination),
        )
    else:
        make_call = partial(
            uploader.send_document,
            target,
            file_path,
            caption=media_cap,
            **entity_kwargs(media_cap, caption=True),
            thumb=thumb,
            progress=progress,
            **_destination_kwargs(destination),
        )

    return await _send(make_call, task, parts=[f"media:{message.id}"])
