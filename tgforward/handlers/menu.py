"""分级导航：提取、设置、账号；管理员额外显示管理入口。"""

import asyncio
import logging

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton as Button
from pyrogram.types import InlineKeyboardMarkup as Keyboard

from tgforward.config import OWNER_ID
from tgforward.storage.users import is_whitelisted
from tgforward.telegram.clients import bot
from tgforward.ui import dialogue
from tgforward.ui.i18n import current_language, language_context, set_language, tr
from tgforward.ui.interaction import MessageView, interaction

logger = logging.getLogger(__name__)


def button_label(label, data):
    icons = {
        "nav:telegram": "📱",
        "nav:helper": "🤖",
        "nav:do:me": "👤",
        "nav:do:history": "🕘",
        "nav:do:login": "🔐",
        "nav:confirm:logout": "🚪",
        "nav:do:bindbot": "🔗",
        "nav:confirm:unbindbot": "🔓",
        "nav:do:status": "📊",
        "nav:do:allow": "➕",
        "nav:do:ban": "➖",
        "set:rename": "🏷️",
        "set:thumb": "🖼️",
        "set:caption": "💬",
        "set:replacement": "🔄",
        "set:deleteword": "✂️",
        "set:remthumb": "🗑️",
        "set:comments": "💬",
        "set:confirmreset": "♻️",
        "set:reset": "♻️",
    }
    if label.startswith("🔴 "):
        label = label[2:]
    if label and (ord(label[0]) > 0xFFFF or 0x2300 <= ord(label[0]) <= 0x2BFF):
        return label
    icon = icons.get(data, "📋" if data.startswith("nav:list:") else "✅")
    return f"{icon} {label}"


def keyboard(rows, back="home"):
    result = [
        [Button(button_label(label, data), callback_data=data) for label, data in row]
        for row in rows
    ]
    if back is not None:
        result.append(
            [
                Button(
                    tr("↩️ 返回"),
                    callback_data=back if back.startswith(("nav:", "set:")) else f"nav:{back}",
                )
            ]
        )
    result.append([Button(tr("✖️ 关闭菜单"), callback_data="nav:close")])
    return Keyboard(result)


def page(name, uid, *, language=None):
    if language is not None:
        with language_context(language):
            return page(name, uid)
    if name == "language":
        return (
            tr("menu.language"),
            keyboard(
                [
                    [
                        (
                            "✓ 简体中文" if current_language() == "zh" else "简体中文",
                            "nav:language:zh",
                        )
                    ],
                    [("✓ English" if current_language() == "en" else "English", "nav:language:en")],
                ]
            ),
        )
    if name == "home":
        rows = [
            [(tr("⚙️ 提取设置"), "nav:settings")],
            [(tr("👤 账号与记录"), "nav:account")],
        ]
        if uid in OWNER_ID:
            rows.append([(tr("🛠 用户管理"), "nav:admin")])
        return (
            tr("menu.welcome_to_tgforward_extract_messages"),
            keyboard(rows + [[(tr("🌐 语言"), "nav:language")]], None),
        )
    if name == "account":
        return (
            tr("👤 **账号与记录**\n\n管理登录账号、辅助机器人，并查看已经提取过的内容。"),
            keyboard(
                [
                    [(tr("Telegram 账号"), "nav:telegram"), (tr("辅助机器人"), "nav:helper")],
                    [(tr("我的状态"), "nav:do:me"), (tr("提取记录"), "nav:do:history")],
                ]
            ),
        )
    if name == "telegram":
        return (
            tr("📱 **Telegram 账号**\n\n按提示输入手机号、验证码及两步验证密码（如有）。"),
            keyboard(
                [[(tr("登录账号"), "nav:do:login"), (tr("退出登录"), "nav:confirm:logout")]],
                "account",
            ),
        )
    if name == "helper":
        return (
            tr("menu.helper_bot_send_the_token"),
            keyboard(
                [
                    [
                        (tr("绑定 / 更换"), "nav:do:bindbot"),
                        (tr("解除绑定"), "nav:confirm:unbindbot"),
                    ]
                ],
                "account",
            ),
        )
    if name == "admin" and uid in OWNER_ID:
        return (
            tr("🛠 **用户管理**\n\n白名单控制使用权限；管理员始终具有权限。"),
            keyboard(
                [
                    [(tr("白名单列表"), "nav:list:0")],
                    [(tr("运行状态"), "nav:do:status")],
                ]
            ),
        )
    return None


async def show_home(message):
    from tgforward.ui.panel import dismiss_keyboard

    await dismiss_keyboard(message)
    panel = getattr(message, "_panel", None)
    if panel:
        panel.parent = None
    await dialogue.clear(message.from_user.id, keep=panel.message if panel else None)
    text, markup = page("home", message.from_user.id, language=current_language())
    await message.reply(text, reply_markup=markup)


@bot.on_callback_query(filters.regex(r"^nav:"))
@interaction
async def navigate(client, query):
    uid = query.from_user.id
    if not query.message or query.message.chat.id != uid or not await is_whitelisted(uid):
        await query.answer(tr("请在有使用权限的账号私聊中操作。"), show_alert=True)
        return
    action = query.data[4:]
    panel = query.message._panel
    language = current_language()
    if action in ("language:en", "language:zh", "home:en", "home:zh"):
        from tgforward.storage.users import set_ui_language

        language = action.split(":")[1]
        if not await set_ui_language(uid, language):
            await query.answer(tr("保存失败，请重试。"), show_alert=True)
            return
        if panel:
            panel.ui_language = language
        set_language(language)
        from tgforward.handlers.start import configure_user_commands

        try:
            await asyncio.wait_for(configure_user_commands(uid, language), timeout=5)
        except Exception:
            logger.exception("Command menu language update failed user=%s", uid)
        action = "home" if action.startswith("home:") else "language"
    await dialogue.clear(uid, keep=panel.message if panel else None)
    if panel:
        panel.parent = {
            "home": None,
            "account": "home",
            "language": "home",
            "telegram": "account",
            "helper": "account",
            "admin": "home",
            "settings": "home",
        }.get(action, panel.parent)
    if action == "close":
        await query.answer(tr("菜单已关闭"))
        await dialogue.clear(uid)
        if panel:
            await panel.close()
        else:
            await query.message.delete()
        return
    if action.startswith("list:"):
        from tgforward.handlers.admin import whitelist_page

        if uid not in OWNER_ID:
            await query.answer(tr("仅管理员可查看白名单。"), show_alert=True)
            return
        if panel:
            panel.parent = "admin"
        await dialogue.clear(uid, keep=panel.message if panel else None)
        try:
            text, markup = await whitelist_page(int(action.split(":")[1]))
        except ValueError:
            await query.answer(tr("页码无效"))
            return
        await query.answer()
        await query.message.edit(text, reply_markup=markup)
        return
    if action.startswith("confirm:"):
        command = action.split(":")[1]
        labels = {"logout": tr("退出 Telegram 账号登录"), "unbindbot": tr("解除辅助机器人绑定")}
        if command not in labels:
            await query.answer(tr("操作无效"))
            return
        await query.answer()
        await query.message.edit(
            tr("⚠️ **确认操作**\n\n即将{0}。\n已提取的消息和文件会保留。", labels[command]),
            reply_markup=keyboard(
                [[(tr("确认"), f"nav:do:{command}")]],
                "telegram" if command == "logout" else "helper",
            ),
        )
        return
    if action == "settings" or action.startswith("do:"):
        from tgforward.handlers import admin, auth, relay, settings, start

        command = "setting" if action == "settings" else action[3:]
        commands = {
            "setting": settings.settings_command,
            "login": auth.login_command,
            "logout": auth.logout_command,
            "bindbot": relay.bind_bot,
            "unbindbot": relay.unbind_bot,
            "allow": admin.allow_user,
            "ban": admin.ban_user,
            "status": admin.status_handler,
            "me": admin.me_handler,
            "history": start.history_handler,
        }
        handler = commands.get(command)
        if handler is None:
            await query.answer(tr("操作无效"))
            return
        await query.answer()
        if panel:
            panel.parent = {
                "setting": "home",
                "login": "telegram",
                "logout": "telegram",
                "bindbot": "helper",
                "unbindbot": "helper",
                "allow": "list:0",
                "ban": "list:0",
                "status": "admin",
                "me": "account",
                "history": "account",
            }[command]
        await dialogue.clear(uid, keep=panel.message if panel else None)
        message = MessageView(
            query.message, query.message._messages, actor=query.from_user, command=[command]
        )
        await handler(client, message)
        return
    result = page(action, uid, language=language)
    if result is None:
        await query.answer(tr("菜单不可用"))
        return
    await query.answer()
    await dialogue.clear(uid, keep=query.message)
    await query.message.edit(result[0], reply_markup=result[1])
