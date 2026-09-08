"""/start /help /set —— 入口指令与机器人命令注册。"""

from pyrogram import filters, raw
from pyrogram.types import BotCommand, BotCommandScopeChat

from tgforward.config import OWNER_ID
from tgforward.handlers.common import ensure_whitelisted
from tgforward.storage.users import get_user
from tgforward.telegram.clients import bot
from tgforward.ui.i18n import language_context, tr
from tgforward.ui.interaction import interaction

BOT_COMMANDS = [
    BotCommand("start", "主菜单"),
    BotCommand("setting", "提取设置"),
    BotCommand("account", "账号与记录"),
]


@bot.on_message(filters.command("id") & filters.private)
@interaction
async def id_handler(client, message):
    """获取用户与对话的数字 ID（配置 OWNER_ID 时有用）。"""
    await message.reply(
        tr("🆔 你的用户 ID：`{0}`\n💬 当前对话 ID：`{1}`", message.from_user.id, message.chat.id)
    )


@bot.on_message(filters.command("history") & filters.private)
@interaction
async def history_handler(client, message):
    """查看自己的提取历史（最近 20 条）。"""
    if not await ensure_whitelisted(message):
        return

    doc = await get_user(message.from_user.id) or {}
    history = doc.get("history") or []
    if not history:
        await message.reply(tr("📋 还没有提取记录，发送消息链接或直接转发消息即可开始。"))
        return

    lines = [tr("🕘 **提取历史**"), tr("\n最近 20 条记录：")]
    for item in reversed(history):
        chat_ref, msg_id = item.get("c"), item.get("m")
        if item.get("p") and str(chat_ref).startswith("-100"):
            url = f"https://t.me/c/{str(chat_ref)[4:]}/{msg_id}"
        else:
            url = f"https://t.me/{chat_ref}/{msg_id}"
        label = str(item.get("s") or tr("提取")).replace("[", "(").replace("]", ")")
        lines.append(f"• [{label}]({url})")
    await message.reply("\n".join(lines))


@bot.on_message((filters.command("start") | filters.regex(r"^📋 菜单$")) & filters.private)
@interaction
async def start_handler(client, message):
    if not await ensure_whitelisted(message):
        return
    from tgforward.handlers.menu import show_home

    await show_home(message)


@bot.on_message(filters.command("help") & filters.private)
@interaction
async def help_handler(client, message):
    if not await ensure_whitelisted(message):
        return
    from tgforward.handlers.menu import show_home

    await show_home(message)


@bot.on_message(filters.command("set") & filters.private)
@interaction
async def set_commands(client, message):
    if message.from_user.id not in OWNER_ID:
        await message.reply(tr("⚠️ 仅管理员可使用此指令。"))
        return
    await configure_menu()
    await message.reply(tr("✅ 指令列表已更新！"))


async def configure_menu():
    with language_context("en"):
        commands = [BotCommand(item.command, tr(item.description)) for item in BOT_COMMANDS]
    await bot.set_bot_commands(commands)
    await bot.set_bot_commands(commands, language_code="en")
    await bot.set_bot_commands(BOT_COMMANDS, language_code="zh")
    await bot.invoke(
        raw.functions.bots.SetBotMenuButton(
            user_id=raw.types.InputUserEmpty(),
            button=raw.types.BotMenuButtonCommands(),
        )
    )


@bot.on_message(filters.command("account") & filters.private)
@interaction
async def account_handler(client, message):
    if not await ensure_whitelisted(message):
        return
    from tgforward.handlers.menu import page
    from tgforward.ui import dialogue

    await dialogue.clear(message.from_user.id)
    text, markup = page("account", message.from_user.id)
    await message.reply(text, reply_markup=markup)


async def configure_user_commands(uid, language):
    with language_context(language):
        commands = [BotCommand(item.command, tr(item.description)) for item in BOT_COMMANDS]
    await bot.set_bot_commands(commands, scope=BotCommandScopeChat(chat_id=uid))
