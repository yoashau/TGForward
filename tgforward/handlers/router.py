"""智能路由：识别私信中的提取请求并触发提取。

三种触发方式：
1. 发送 Telegram 消息链接（链接后跟数字表示批量数量，如 `…/100 10`）
2. 直接**转发**频道消息给机器人（受保护频道禁复制链接时的替代方案）
3. 链接写在媒体说明文字（caption）里也能识别

转发相册时 Telegram 会拆成多条消息逐条送达：首条被接受后，其
media_group_id 会被登记 120 秒，同组后续消息静默跳过，避免
"已有任务进行中"的噪音。
"""

import asyncio
import logging
import time

from pyrogram import filters

from tgforward.handlers.common import DENY_TEXT
from tgforward.runtime import diagnostics, lifecycle, tasks
from tgforward.runtime.tasks import TaskAlreadyActive, TaskCancelled, TaskCooldown
from tgforward.storage.users import is_whitelisted
from tgforward.telegram.clients import bot
from tgforward.transfers.extractor import extract_range, extract_single
from tgforward.ui import state
from tgforward.ui.dialogue import cancel_keyboard
from tgforward.ui.i18n import tr
from tgforward.ui.interaction import interaction
from tgforward.utils.links import MessageLink, find_links, parse_batch_count, parse_link

logger = logging.getLogger(__name__)

# 转发相册去重：user_id -> {media_group_id: 过期时间}
_recent_groups: dict[int, dict[str, float]] = {}
_GROUP_TTL = 120.0
_GROUP_CAPACITY = 4096  # 所有用户合计上限，而非每用户无界增长
_group_timer = None


def _expire_groups():
    global _group_timer
    _group_timer = None
    now = time.monotonic()
    for uid, entry in list(_recent_groups.items()):
        for gid, expiry in list(entry.items()):
            if expiry <= now:
                entry.pop(gid, None)
        if not entry:
            _recent_groups.pop(uid, None)
    _schedule_group_expiry()


def _schedule_group_expiry():
    global _group_timer
    if not _recent_groups:
        return
    if (
        _group_timer is not None
        and not _group_timer.cancelled()
        and not _group_timer._loop.is_closed()
    ):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 同步调用仍受到容量约束，生产路由总在运行中的循环内。
    next_expiry = min(expiry for entry in _recent_groups.values() for expiry in entry.values())
    _group_timer = loop.call_later(max(0, next_expiry - time.monotonic()), _expire_groups)


def purge_user(uid):
    _recent_groups.pop(int(uid), None)


async def shutdown():
    global _group_timer
    if _group_timer is not None:
        _group_timer.cancel()
        _group_timer = None
    _recent_groups.clear()


def _ref_from_forward(message) -> MessageLink | None:
    """从转发消息中定位源消息。无法确定来源时返回 None。"""
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        src = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        mid = getattr(origin, "message_id", None)
    else:
        src = getattr(message, "forward_from_chat", None)
        mid = getattr(message, "forward_from_message_id", None)
    if src is None or not mid:
        return None
    if getattr(src, "username", None):
        return MessageLink(src.username, mid, False)
    sid = str(src.id)
    if sid.startswith("-100"):
        return MessageLink(sid, mid, True)
    return None  # 基础群组等不支持直接按 ID 提取的来源


def _group_recently_handled(uid: int, gid: str | None) -> bool:
    if not gid:
        return False
    expiry = _recent_groups.get(uid, {}).get(gid, 0)
    return time.monotonic() < expiry


def _mark_group_handled(uid: int, gid: str | None) -> None:
    if not gid:
        return
    _recent_groups.setdefault(uid, {})[gid] = time.monotonic() + _GROUP_TTL
    while sum(map(len, _recent_groups.values())) > _GROUP_CAPACITY:
        oldest_uid, oldest_gid = min(
            ((u, g) for u, entry in _recent_groups.items() for g in entry),
            key=lambda key: _recent_groups[key[0]][key[1]],
        )
        _recent_groups[oldest_uid].pop(oldest_gid)
        if not _recent_groups[oldest_uid]:
            _recent_groups.pop(oldest_uid)
    _schedule_group_expiry()


def _deduplicate_plan(plan, message):
    origin = getattr(message, "forward_origin", None)
    src = (
        getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if origin is not None
        else getattr(message, "forward_from_chat", None)
    )
    username = str(getattr(src, "username", "") or "").lower()
    source_id = str(getattr(src, "id", ""))
    unique, positions = [], {}
    for ref, count, url in plan:
        chat = str(ref.chat).lstrip("@").lower()
        if username and chat == username:
            chat = source_id  # 转发元数据提供的 username / 数字 ID 别名。
        if chat.lstrip("-").isdigit():
            chat = str(int(chat))
        key = (chat, ref.message_id, ref.comment_id)
        if key in positions:
            index = positions[key]
            old_ref, old_count, old_url = unique[index]
            unique[index] = (old_ref, max(old_count, count), old_url)
        else:
            positions[key] = len(unique)
            unique.append((ref, count, url))
    return unique


def _display_url(ref: MessageLink) -> str:
    if ref.is_private and str(ref.chat).startswith("-100"):
        return f"https://t.me/c/{str(ref.chat)[4:]}/{ref.message_id}"
    return f"https://t.me/{ref.chat}/{ref.message_id}"


@bot.on_message(filters.private & ~state.user_busy)
@interaction
@lifecycle.serialized
async def smart_router(client, message):
    if message.from_user is None:
        return
    uid = message.from_user.id

    if state.is_busy(uid) or uid in lifecycle.revoked:
        return

    # ── 触发源 1：转发消息 ──
    forward_ref = _ref_from_forward(message)

    # ── 触发源 2：文本 / 媒体说明文字中的链接 ──
    text = message.text or message.caption or ""
    if not forward_ref and (not text or text.lstrip().startswith("/")):
        return  # 无转发、无链接（含所有指令），静默忽略

    urls = find_links(text)

    if not forward_ref and not urls:
        return

    if not await is_whitelisted(uid):
        await message.reply(DENY_TEXT)
        return

    plan = []
    if forward_ref:
        plan.append((forward_ref, 1, _display_url(forward_ref)))
    for url in urls:
        ref = parse_link(url)
        if ref is None:
            continue
        plan.append((ref, parse_batch_count(text, url), url))

    plan = _deduplicate_plan(plan, message)

    if not plan:
        if forward_ref is None:
            await message.reply(tr("⚠️ 未能识别有效的 Telegram 消息链接。"))
        return

    gid = getattr(message, "media_group_id", None)
    if _group_recently_handled(uid, gid):
        return  # 同一转发相册的后续成员，静默跳过

    total = sum(count for _, count, _ in plan)
    try:
        task = tasks.register(uid, "batch", total)
    except TaskAlreadyActive:
        await message.reply(
            tr(
                "tasks.busy",
                tr(tasks.get(uid).stage),
            ),
            reply_markup=cancel_keyboard(uid),
        )
        return
    except TaskCooldown as e:
        await message.reply(tr("⏳ 操作太频繁，请 {0} 秒后再试。", int(e.remaining) + 1))
        return

    _mark_group_handled(uid, gid)

    tasks.launch(task, lambda: _run_plan(message, plan, task), message.reply)


async def _run_plan(message, plan, task):
    try:
        for i, (ref, count, url) in enumerate(plan):
            task.active_unit = task.extraction_unit((i, url), count)
            try:
                if count > 1:
                    await extract_range(message, ref, count, task=task)
                else:
                    await extract_single(message, ref, task=task)
            except TaskCancelled:
                break
            except Exception as e:
                logger.exception("处理链接出错 %s: %s", url, e)
                diagnostics.record_error("router", str(e))
                await message.reply(
                    tr("⚠️ 处理链接时出错：`{0}`\n错误信息：{1}", url[:80], str(e)[:100])
                )

            task.active_unit = None
            if task.cancelled:
                await message.reply(tr("🚫 已取消。"))
                break
            if len(plan) > 1 and i < len(plan) - 1:
                await task.wait_or_cancel(2, tr("多链接提取间隔"))
    finally:
        if task.cancelled or task.timed_out:
            _recent_groups.get(message.from_user.id, {}).pop(
                getattr(message, "media_group_id", None),
                None,
            )
        tasks.finish(message.from_user.id, task)
