"""分级导航：提取、设置、账号；管理员额外显示管理入口。"""

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton as Button
from pyrogram.types import InlineKeyboardMarkup as Keyboard

from tgforward.config import OWNER_ID
from tgforward.storage.users import is_whitelisted
from tgforward.telegram.clients import bot
from tgforward.ui import dialogue
from tgforward.ui.interaction import MessageView, interaction


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
                    "↩️ 返回",
                    callback_data=back if back.startswith(("nav:", "set:")) else f"nav:{back}",
                )
            ]
        )
    result.append([Button("✖️ 关闭菜单", callback_data="nav:close")])
    return Keyboard(result)


def page(name, uid):
    if name == "home":
        rows = [
            [("⚙️ 提取设置", "nav:settings")],
            [("👤 账号与记录", "nav:account")],
        ]
        if uid in OWNER_ID:
            rows.append([("🛠 用户管理", "nav:admin")])
        return (
            "🤖 **欢迎使用 TGForward**\n\n"
            "📩 **提取消息**\n发送 Telegram 消息链接，或直接转发频道消息。\n\n"
            "📚 **批量提取**\n链接后加空格和数量，例如：\n"
            "`https://t.me/channel/100 10`\n\n"
            "🔐 **私有来源**\n先在「账号与记录」登录能访问该频道或群组的账号。\n\n"
            "💬 **评论提取**\n"
            "未开启「同时提取评论」时，可在提取完成后的任务管理消息中"
            "点击「💬 提取评论」手动提取。\n"
            "开启后，原帖内容发送完成即自动提取评论。",
            keyboard(rows, None),
        )
    if name == "account":
        return (
            "👤 **账号与记录**\n\n管理登录账号、辅助机器人，并查看已经提取过的内容。",
            keyboard(
                [
                    [("Telegram 账号", "nav:telegram"), ("辅助机器人", "nav:helper")],
                    [("我的状态", "nav:do:me"), ("提取记录", "nav:do:history")],
                ]
            ),
        )
    if name == "telegram":
        return (
            "📱 **Telegram 账号**\n\n按提示输入手机号、验证码及两步验证密码（如有）。",
            keyboard(
                [[("登录账号", "nav:do:login"), ("退出登录", "nav:confirm:logout")]], "account"
            ),
        )
    if name == "helper":
        return (
            "🤖 **辅助机器人**\n\n绑定时按提示发送 BotFather 提供的 Token。\n"
            "私聊请先向辅助机器人发送 /start；群组或频道请添加它并授予发送权限。\n"
            "辅助机器人需能向发送目标发消息；绑定本身不会提高单文件上传上限。",
            keyboard(
                [[("绑定 / 更换", "nav:do:bindbot"), ("解除绑定", "nav:confirm:unbindbot")]],
                "account",
            ),
        )
    if name == "admin" and uid in OWNER_ID:
        return (
            "🛠 **用户管理**\n\n白名单控制使用权限；管理员始终具有权限。",
            keyboard(
                [
                    [("白名单列表", "nav:list:0")],
                    [("运行状态", "nav:do:status")],
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
    text, markup = page("home", message.from_user.id)
    await message.reply(text, reply_markup=markup)


@bot.on_callback_query(filters.regex(r"^nav:"))
@interaction
async def navigate(client, query):
    uid = query.from_user.id
    if not query.message or query.message.chat.id != uid or not await is_whitelisted(uid):
        await query.answer("请在有使用权限的账号私聊中操作。", show_alert=True)
        return
    action = query.data[4:]
    panel = query.message._panel
    await dialogue.clear(uid, keep=panel.message if panel else None)
    if panel:
        panel.parent = {
            "home": None,
            "account": "home",
            "telegram": "account",
            "helper": "account",
            "admin": "home",
            "settings": "home",
        }.get(action, panel.parent)
    if action == "close":
        await query.answer("菜单已关闭")
        await dialogue.clear(uid)
        if panel:
            await panel.close()
        else:
            await query.message.delete()
        return
    if action.startswith("list:"):
        from tgforward.handlers.admin import whitelist_page

        if uid not in OWNER_ID:
            await query.answer("仅管理员可查看白名单。", show_alert=True)
            return
        if panel:
            panel.parent = "admin"
        await dialogue.clear(uid, keep=panel.message if panel else None)
        try:
            text, markup = await whitelist_page(int(action.split(":")[1]))
        except ValueError:
            await query.answer("页码无效")
            return
        await query.answer()
        await query.message.edit(text, reply_markup=markup)
        return
    if action.startswith("confirm:"):
        command = action.split(":")[1]
        labels = {"logout": "退出 Telegram 账号登录", "unbindbot": "解除辅助机器人绑定"}
        if command not in labels:
            await query.answer("操作无效")
            return
        await query.answer()
        await query.message.edit(
            f"⚠️ **确认操作**\n\n即将{labels[command]}。\n已提取的消息和文件会保留。",
            reply_markup=keyboard(
                [[("确认", f"nav:do:{command}")]], "telegram" if command == "logout" else "helper"
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
            await query.answer("操作无效")
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
    result = page(action, uid)
    if result is None:
        await query.answer("菜单不可用")
        return
    await query.answer()
    await dialogue.clear(uid, keep=query.message)
    await query.message.edit(result[0], reply_markup=result[1])
