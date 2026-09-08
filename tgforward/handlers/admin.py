"""管理员指令：/allow /ban /me /status /broadcast。"""

import asyncio
import contextlib
import functools
from types import SimpleNamespace

from pyrogram import filters

from tgforward import __version__
from tgforward.config import OWNER_ID
from tgforward.handlers.common import ensure_whitelisted
from tgforward.runtime import diagnostics, tasks
from tgforward.storage.users import (
    count_whitelisted,
    delete_user,
    get_user,
    is_whitelisted,
    list_whitelisted_ids,
    list_whitelisted_page,
    set_whitelisted,
    storage_healthy,
)
from tgforward.telegram import clients as clients_registry
from tgforward.telegram.clients import bot, uptime_text
from tgforward.ui import dialogue, state
from tgforward.ui.i18n import language_context, tr, user_language
from tgforward.ui.interaction import interaction


def owner_only(func):
    """仅允许 OWNER_ID 使用的装饰器。"""

    @functools.wraps(func)
    async def wrapper(client, message):
        if message.from_user.id not in OWNER_ID:
            await message.reply(tr("⚠️ 仅管理员可使用此指令。"))
            return
        return await func(client, message)

    return wrapper


async def _admin_prompt(message, action):
    text = (
        tr("👤 请发送要加入白名单的用户数字 ID。")
        if action == "allow"
        else tr("👤 请发送要移出白名单的用户数字 ID（其会话和设置也将删除）。")
    )
    return await dialogue.begin(message, "admin", action, text)


@bot.on_message(filters.command("allow") & filters.private)
@interaction
@owner_only
async def allow_user(client, message):
    st = await _admin_prompt(message, "allow")
    if len(message.command) > 1 and state.get(message.from_user.id) is st:
        await _apply_user_change(client, message, message.command[1])


@bot.on_message(filters.command("ban") & filters.private)
@interaction
@owner_only
async def ban_user(client, message):
    st = await _admin_prompt(message, "ban")
    if len(message.command) > 1 and state.get(message.from_user.id) is st:
        await _apply_user_change(client, message, message.command[1])


@bot.on_message(state.admin_active & filters.private & state.non_command)
@interaction
@owner_only
async def admin_user_input(client, message):
    await _apply_user_change(client, message, (message.text or "").strip())


@dialogue.input_guard("admin")
async def _apply_user_change(client, message, raw):
    uid = message.from_user.id
    st = state.get(uid)
    if st is None or st.kind != "admin":
        return
    if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
        await dialogue.prompt(message, tr("❌ 请发送正整数用户 ID；不是用户名，也不是消息链接。"))
        return
    target_id = int(raw)
    if st.step == "allow":
        saved = await set_whitelisted(target_id, True)
        if saved:
            await message.reply(tr("✅ 用户 `{0}` 已加入白名单。", target_id))
            with contextlib.suppress(Exception):
                language = await user_language(SimpleNamespace(id=target_id))
                with language_context(language):
                    await client.send_message(
                        target_id, tr("✅ 你已加入白名单，发送 /start 开始使用。")
                    )
    else:
        if target_id in OWNER_ID:
            await dialogue.prompt(message, tr("❌ 管理员无需移除，请输入其他用户 ID。"))
            return
        saved = await delete_user(target_id)
        if saved:
            await message.reply(tr("✅ 用户 `{0}` 已移出白名单，其数据已全部清除。", target_id))
    if saved:
        if state.get(uid) is st:
            state.clear(uid)
    else:
        await dialogue.prompt(message, tr("⚠️ 操作未全部完成，请重新发送用户 ID 检查并重试。"))


@bot.on_message(filters.command("status") & filters.private)
@interaction
@owner_only
async def status_handler(client, message):
    lines = [f"🤖 **TGForward** v{__version__}", tr("⏱ 运行时长：{0}", uptime_text())]

    active = tasks.all_active()
    if active:
        labels = {"single": tr("单条提取"), "batch": tr("批量提取"), "comments": tr("评论提取")}
        parts = [f"{labels.get(t.kind, t.kind)} {t.current}/{t.total}" for t in active.values()]
        lines.append(tr("📋 进行中任务：") + "、".join(parts))
    else:
        lines.append(tr("📋 进行中任务：无"))

    lines.append(
        tr("💎 Premium 通道：")
        + (tr("✅ 可用") if clients_registry.premium_started else tr("❌ 未启用"))
    )

    try:
        await asyncio.wait_for(storage_healthy(), timeout=3)
        lines.append(tr("🗄️ 持久化状态：✅ 正常"))
    except Exception:
        lines.append(tr("🗄️ 持久化状态：❌ 异常"))

    errs = diagnostics.recent_errors(3)
    if errs:
        lines.append(tr("⚠️ 最近错误："))
        for e in errs:
            lines.append(f"• `{diagnostics.format_time(e['t'])}` {e['source']}：{e['msg'][:60]}")
    else:
        lines.append(tr("⚠️ 最近错误：无"))

    await message.reply("\n".join(lines))


@bot.on_message(filters.command("broadcast") & filters.private)
@interaction
@owner_only
async def broadcast_handler(client, message):
    parts = (message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(tr("用法：/broadcast `<文本>`\n向全部白名单用户发送一条消息。"))
        return

    text = parts[1].strip()
    user_ids = await list_whitelisted_ids()
    status = await message.reply(tr("📣 开始向 {0} 个白名单用户广播……", len(user_ids)))

    ok = fail = 0
    for user_id in user_ids:
        try:
            await client.send_message(user_id, text)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.1)  # 轻微限速，避免触发 flood

    await status.edit(tr("✅ 广播完成：成功 {0}，失败 {1}", ok, fail))


@bot.on_message(filters.command("me") & filters.private)
@interaction
async def me_handler(client, message):
    if not await ensure_whitelisted(message):
        return

    uid = message.from_user.id
    doc = await get_user(uid) or {}
    is_owner = uid in OWNER_ID
    has_session = bool(doc.get("session_string"))
    has_bot = bool(doc.get("bot_token"))
    has_target = bool(doc.get("chat_id"))
    has_caption = bool(doc.get("caption"))

    lines = [
        tr("👤 **个人状态**"),
        "",
        tr("👑 管理员") if is_owner else tr("✅ 白名单用户"),
        tr("**Telegram 会话：** {0}", tr("✅ 已登录") if has_session else tr("❌ 未登录")),
        tr("**辅助机器人：** {0}", tr("✅ 已绑定") if has_bot else tr("❌ 未绑定")),
    ]
    if is_owner or await is_whitelisted(uid):
        extras = []
        if has_target:
            extras.append(tr("发送目标 `{0}`", doc.get("chat_id")))
        if has_caption:
            extras.append(tr("已设置追加文案"))
        if doc.get("rename_tag"):
            extras.append(tr("文件名标签 `{0}`", doc.get("rename_tag")))
        if extras:
            lines.append(tr("**个性化：** ") + "，".join(extras))

    stats = doc.get("stats") or {}
    if stats.get("extracts"):
        lines.append(tr("**累计提取：** {0} 次", stats["extracts"]))

    history = doc.get("history") or []
    if history:
        lines.append(tr("\n🕘 **最近提取**"))
        for item in reversed(history[-5:]):
            chat_ref, msg_id = item.get("c"), item.get("m")
            if item.get("p") and str(chat_ref).startswith("-100"):
                url = f"https://t.me/c/{str(chat_ref)[4:]}/{msg_id}"
            else:
                url = f"https://t.me/{chat_ref}/{msg_id}"
            label = str(item.get("s") or tr("提取")).replace("[", "(").replace("]", ")")
            lines.append(f"• [{label}]({url})")

    await message.reply("\n".join(lines))


async def whitelist_page(page_number=0):
    from tgforward.handlers.menu import keyboard

    size = 20
    count = await count_whitelisted(exclude_owners=True)
    pages = max(1, (count + size - 1) // size)
    page_number = max(0, min(page_number, pages - 1))
    user_ids = await list_whitelisted_page(page_number * size, size, exclude_owners=True)
    lines = [
        tr("📋 **白名单 · 第 {0}/{1} 页**", page_number + 1, pages),
        "",
        tr("管理员（始终有权限）：") + "、".join(f"`{uid}`" for uid in sorted(OWNER_ID)),
        tr("普通成员：{0} 人", count),
        "",
    ]
    lines += [f"• `{user_id}`" for user_id in user_ids] or [tr("暂无普通成员。")]
    nav = []
    if page_number:
        nav.append((tr("‹ 上一页"), f"nav:list:{page_number - 1}"))
    if page_number + 1 < pages:
        nav.append((tr("下一页 ›"), f"nav:list:{page_number + 1}"))
    rows = [nav] if nav else []
    rows.append([(tr("添加用户"), "nav:do:allow"), (tr("移除用户"), "nav:do:ban")])
    return "\n".join(lines), keyboard(rows, "admin")


@bot.on_message(filters.command("whitelist") & filters.private)
@interaction
@owner_only
async def whitelist_command(client, message):
    text, markup = await whitelist_page()
    await message.reply(text, reply_markup=markup)
