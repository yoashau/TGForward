"""管理员指令：/allow /ban /me /status /broadcast。"""

import asyncio
import contextlib
import functools

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
from tgforward.ui.interaction import interaction


def owner_only(func):
    """仅允许 OWNER_ID 使用的装饰器。"""

    @functools.wraps(func)
    async def wrapper(client, message):
        if message.from_user.id not in OWNER_ID:
            await message.reply("⚠️ 仅管理员可使用此指令。")
            return
        return await func(client, message)

    return wrapper


async def _admin_prompt(message, action):
    text = (
        "👤 请发送要加入白名单的用户数字 ID。"
        if action == "allow"
        else "👤 请发送要移出白名单的用户数字 ID（其会话和设置也将删除）。"
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
        await dialogue.prompt(message, "❌ 请发送正整数用户 ID；不是用户名，也不是消息链接。")
        return
    target_id = int(raw)
    if st.step == "allow":
        saved = await set_whitelisted(target_id, True)
        if saved:
            await message.reply(f"✅ 用户 `{target_id}` 已加入白名单。")
            with contextlib.suppress(Exception):
                await client.send_message(target_id, "✅ 你已加入白名单，发送 /start 开始使用。")
    else:
        if target_id in OWNER_ID:
            await dialogue.prompt(message, "❌ 管理员无需移除，请输入其他用户 ID。")
            return
        saved = await delete_user(target_id)
        if saved:
            await message.reply(f"✅ 用户 `{target_id}` 已移出白名单，其数据已全部清除。")
    if saved:
        if state.get(uid) is st:
            state.clear(uid)
    else:
        await dialogue.prompt(message, "⚠️ 操作未全部完成，请重新发送用户 ID 检查并重试。")


@bot.on_message(filters.command("status") & filters.private)
@interaction
@owner_only
async def status_handler(client, message):
    lines = [f"🤖 **TGForward** v{__version__}", f"⏱ 运行时长：{uptime_text()}"]

    active = tasks.all_active()
    if active:
        labels = {"single": "单条提取", "batch": "批量提取", "comments": "评论提取"}
        parts = [f"{labels.get(t.kind, t.kind)} {t.current}/{t.total}" for t in active.values()]
        lines.append("📋 进行中任务：" + "、".join(parts))
    else:
        lines.append("📋 进行中任务：无")

    lines.append(
        "💎 Premium 通道：" + ("✅ 可用" if clients_registry.premium_started else "❌ 未启用")
    )

    try:
        await asyncio.wait_for(storage_healthy(), timeout=3)
        lines.append("🗄️ 持久化状态：✅ 正常")
    except Exception:
        lines.append("🗄️ 持久化状态：❌ 异常")

    errs = diagnostics.recent_errors(3)
    if errs:
        lines.append("⚠️ 最近错误：")
        for e in errs:
            lines.append(f"• `{diagnostics.format_time(e['t'])}` {e['source']}：{e['msg'][:60]}")
    else:
        lines.append("⚠️ 最近错误：无")

    await message.reply("\n".join(lines))


@bot.on_message(filters.command("broadcast") & filters.private)
@interaction
@owner_only
async def broadcast_handler(client, message):
    parts = (message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply("用法：/broadcast `<文本>`\n向全部白名单用户发送一条消息。")
        return

    text = parts[1].strip()
    user_ids = await list_whitelisted_ids()
    status = await message.reply(f"📣 开始向 {len(user_ids)} 个白名单用户广播……")

    ok = fail = 0
    for user_id in user_ids:
        try:
            await client.send_message(user_id, text)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.1)  # 轻微限速，避免触发 flood

    await status.edit(f"✅ 广播完成：成功 {ok}，失败 {fail}")


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
        "👤 **个人状态**",
        "",
        "👑 管理员" if is_owner else "✅ 白名单用户",
        f"**Telegram 会话：** {'✅ 已登录' if has_session else '❌ 未登录'}",
        f"**辅助机器人：** {'✅ 已绑定' if has_bot else '❌ 未绑定'}",
    ]
    if is_owner or await is_whitelisted(uid):
        extras = []
        if has_target:
            extras.append(f"发送目标 `{doc.get('chat_id')}`")
        if has_caption:
            extras.append("已设置追加文案")
        if doc.get("rename_tag"):
            extras.append(f"文件名标签 `{doc.get('rename_tag')}`")
        if extras:
            lines.append("**个性化：** " + "，".join(extras))

    stats = doc.get("stats") or {}
    if stats.get("extracts"):
        lines.append(f"**累计提取：** {stats['extracts']} 次")

    history = doc.get("history") or []
    if history:
        lines.append("\n🕘 **最近提取**")
        for item in reversed(history[-5:]):
            chat_ref, msg_id = item.get("c"), item.get("m")
            if item.get("p") and str(chat_ref).startswith("-100"):
                url = f"https://t.me/c/{str(chat_ref)[4:]}/{msg_id}"
            else:
                url = f"https://t.me/{chat_ref}/{msg_id}"
            label = str(item.get("s") or "提取").replace("[", "(").replace("]", ")")
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
        f"📋 **白名单 · 第 {page_number + 1}/{pages} 页**",
        "",
        "管理员（始终有权限）：" + "、".join(f"`{uid}`" for uid in sorted(OWNER_ID)),
        f"普通成员：{count} 人",
        "",
    ]
    lines += [f"• `{user_id}`" for user_id in user_ids] or ["暂无普通成员。"]
    nav = []
    if page_number:
        nav.append(("‹ 上一页", f"nav:list:{page_number - 1}"))
    if page_number + 1 < pages:
        nav.append(("下一页 ›", f"nav:list:{page_number + 1}"))
    rows = [nav] if nav else []
    rows.append([("添加用户", "nav:do:allow"), ("移除用户", "nav:do:ban")])
    return "\n".join(lines), keyboard(rows, "admin")


@bot.on_message(filters.command("whitelist") & filters.private)
@interaction
@owner_only
async def whitelist_command(client, message):
    text, markup = await whitelist_page()
    await message.reply(text, reply_markup=markup)
