"""提取设置面板（inline 按钮 + 对话式输入）。"""

import asyncio
import logging
import os
import re
from contextlib import suppress

from pyrogram import filters

from tgforward.handlers.common import ensure_whitelisted
from tgforward.runtime import lifecycle
from tgforward.storage.users import (
    RuleConflict,
    get_user,
    is_whitelisted,
    reset_settings,
    set_field,
    toggle_auto_comments,
    update_word_rules,
)
from tgforward.telegram.clients import bot
from tgforward.ui import dialogue, state
from tgforward.ui.interaction import interaction
from tgforward.utils.media import normalize_thumbnail, remove_custom_thumb, thumbnail_path

logger = logging.getLogger(__name__)

MENU_TEXT = "⚙️ **提取设置**\n\n选择需要调整的内容。"


def settings_keyboard(category="home"):
    from tgforward.handlers.menu import keyboard

    groups = {
        "home": [
            [("💬 评论区内容", "set:page:transfer")],
            [("✏️ 文件与文案", "set:page:content")],
            [("📤 发送目标", "set:chat"), ("♻️ 恢复默认设置", "set:confirmreset")],
        ],
        "transfer": [
            [("同时提取评论：开 / 关", "set:comments")],
        ],
        "content": [
            [("文件名标签", "set:rename"), ("自定义封面", "set:thumb")],
            [("追加文案", "set:caption")],
            [("替换规则", "set:replacement"), ("删词规则", "set:deleteword")],
            [("移除封面", "set:remthumb")],
        ],
    }
    rows = groups.get(category)
    if rows is None:
        return None
    return keyboard(rows, "home" if category == "home" else "set:page:home")


MENU_KEYBOARD = settings_keyboard()

# 设置项 → 对话提示语
PROMPTS = {
    "rename": ("📝 **文件名标签**\n\n请发送你想在文件名末尾附加的标签（如频道名或个人标识）。\n"),
    "chat": (
        "📤 **设置发送目标**\n\n"
        "请发送目标群组或频道的 ID（以 `-100` 开头）。\n"
        "如需发送到群组的某个话题，格式为 `-100群组ID/话题ID`，例如：`-1004783898/12`\n"
    ),
    "caption": ("💬 **设置追加文案**\n\n请发送你想在纯文字消息末尾或媒体说明中追加的文字内容。\n"),
    "replacement": (
        "🔄 **设置替换规则**\n\n格式：`'原词' '替换词'`\n示例：`'Team SPY' '我的频道'`\n"
    ),
    "deleteword": (
        "🗑️ **设置删词规则**\n\n请发送要从文件名、纯文字和媒体说明中删除的词（多个词用空格分隔）。\n"
    ),
    "thumb": ("🖼️ **设置自定义封面**\n\n请直接发送一张图片作为视频或文件的封面。\n"),
}

_TARGET_RE = re.compile(r"-100[1-9][0-9]*(?:/[1-9][0-9]*)?")


@bot.on_message(filters.command("setting") & filters.private)
@interaction
@lifecycle.serialized
async def settings_command(client, message):
    if not await ensure_whitelisted(message):
        return

    await dialogue.clear(message.from_user.id)
    doc = await get_user(message.from_user.id) or {}
    summary = []
    if doc.get("chat_id"):
        summary.append(f"发送目标 `{doc['chat_id']}`")
    if doc.get("caption"):
        summary.append("已设置追加文案")
    if doc.get("rename_tag"):
        summary.append(f"名称标签 `{doc['rename_tag']}`")
    if doc.get("delete_words"):
        summary.append(f"删词 {len(doc['delete_words'])} 个")
    if doc.get("replacement_words"):
        summary.append(f"替换 {len(doc['replacement_words'])} 条")
    summary.append("公开来源直接复制；私有来源下载后发送")
    summary.append("同时提取评论：" + ("已开启" if doc.get("auto_comments") else "已关闭"))

    text = MENU_TEXT + "\n\n📋 **当前设置**\n" + "\n".join(summary)
    await message.reply(text, reply_markup=MENU_KEYBOARD)


def _callback_filter(_, __, query):
    return isinstance(query.data, str) and query.data.startswith("set:")


@bot.on_callback_query(filters.create(_callback_filter))
@interaction
@lifecycle.serialized
async def settings_callback(client, query):
    uid = query.from_user.id
    if not await is_whitelisted(uid):
        await query.answer("⚠️ 你没有使用权限。", show_alert=True)
        return

    if not query.message or query.message.chat.id != uid:
        await query.answer("请在与机器人的私聊中使用设置。", show_alert=True)
        return

    action = query.data[4:]  # 去掉 "set:" 前缀

    from tgforward.handlers.menu import keyboard

    panel = query.message._panel
    await dialogue.clear(uid, keep=panel.message if panel else None)
    if panel:
        panel.parent = {
            "mode": "set:page:transfer",
            "chat": "set:page:home",
            "confirmreset": "set:page:home",
            "reset": "set:page:home",
        }.get(action, "set:page:content")

    if action.startswith("page:"):
        category = action.split(":", 1)[1]
        if settings_keyboard(category) is None:
            await query.answer("这个页面已更新，请重新打开菜单。")
            return
        await dialogue.clear(uid, keep=panel.message if panel else None)
        await query.answer()
        await show_settings_page(query.message, uid, category)
        return
    if action == "confirmreset":
        await query.answer()
        await query.message.edit(
            "**恢复默认设置？**\n文件名、封面、说明文字、保存位置和评论选项都会重置。"
            "\n不会退出账号，也不会删除已提取的内容。",
            reply_markup=keyboard([[("🔴 确认恢复默认", "set:reset")]], "set:page:home"),
        )
        return
    if action == "mode" or action.startswith("mode:"):
        await query.answer("下载选项已取消：公开来源直接复制，私有来源直接下载。", show_alert=True)
        await show_settings_page(query.message, uid, "home")
        return
    if action == "comments":
        enabled = await toggle_auto_comments(uid)
        if enabled is not None:
            await query.answer("已保存")
            await show_settings_page(
                query.message,
                uid,
                "transfer",
                "✅ 同时提取评论已" + ("开启。" if enabled else "关闭。"),
            )
        else:
            await query.answer("保存失败，请重试。", show_alert=True)
        return
    if action == "reset":
        saved = await reset_settings(uid)
        if not saved:
            await query.answer("部分设置未能恢复，请重试。", show_alert=True)
            return
        await dialogue.clear(uid, keep=panel.message if panel else None)
        thumb = remove_custom_thumb(uid)
        if thumb == "failed":
            await query.answer("设置已恢复，但封面删除失败。", show_alert=True)
            await show_settings_page(query.message, uid, "home", "⚠️ 设置已恢复，封面未完全清理。")
            return
        await query.answer("已恢复默认设置")
        await show_settings_page(query.message, uid, "home", "✅ 已恢复默认设置。")
        return
    if action == "remthumb":
        removed = remove_custom_thumb(uid)
        await query.answer(
            {
                "removed": "已移除封面",
                "absent": "还没有设置封面",
                "failed": "封面删除失败，请重新操作。",
            }[removed],
            show_alert=removed == "failed",
        )
        await show_settings_page(query.message, uid, "content")
        return
    prompt = PROMPTS.get(action)
    if prompt is None:
        await query.answer("这个选项已失效，请重新打开菜单。")
        return
    await query.answer()
    await dialogue.begin(query.message, "settings", action, prompt, uid=uid)


async def show_settings_page(message, uid, category, notice=""):
    current = await get_user(uid) or {}
    text = {
        "home": MENU_TEXT,
        "transfer": (
            "💬 **评论区内容**\n\n同时提取评论："
            + ("开启" if current.get("auto_comments") else "关闭")
            + "\n\n开启：正文发送后，自动提取评论文字和媒体。"
            "\n关闭：在提取完成后的任务管理消息中点击「💬 提取评论」手动提取。"
            "\n\n🔐 需要登录能查看该评论区的账号。"
        ),
        "content": "✏️ **文件与文案**\n\n🏷️ 为文件添加标签、更换封面。\n"
        "💬 追加文案，或设置替换、删词规则。\n\n"
        "📌 文件名和封面设置仅作用于下载后上传的文件；公开来源保留原文件名和封面。",
    }[category]
    await message.edit(
        (notice + "\n\n" if notice else "") + text, reply_markup=settings_keyboard(category)
    )


@bot.on_message(state.settings_active & filters.private & state.non_command)
@interaction
@dialogue.input_guard("settings")
@lifecycle.serialized
async def handle_settings_input(client, message):
    uid = message.from_user.id
    st = state.get(uid)
    if st is None or st.kind != "settings":
        return

    # 让 /cancel 等指令正常走各自处理器，不视为设置输入
    if message.text and message.text.startswith("/"):
        return

    action = st.step

    if action == "thumb":
        await _handle_setthumb(message, uid)
        return

    if not message.text:
        await dialogue.prompt(message, "❌ 请发送文本内容。")
        return

    handler = {
        "chat": _handle_setchat,
        "rename": _handle_setrename,
        "caption": _handle_setcaption,
        "replacement": _handle_setreplacement,
        "deleteword": _handle_deleteword,
    }.get(action)

    if handler is None:
        state.clear(uid)
        return

    succeeded = await handler(message, uid)
    if succeeded and state.get(uid) is st:
        state.clear(uid)


async def _handle_setchat(message, uid) -> bool:
    raw = (message.text or "").strip()
    match = _TARGET_RE.fullmatch(raw)
    valid = match is not None and len(raw) <= 32
    if valid:
        chat, _, topic = raw.partition("/")
        valid = -(2**63) < int(chat) < 0 and (not topic or int(topic) < 2**31)
    if not valid:
        await dialogue.prompt(
            message,
            "❌ 格式不正确。应为 `-100频道ID` 或 `-100群组ID/话题ID`，可重新发送或 /cancel 取消。",
        )
        return False
    if await set_field(uid, "chat_id", raw):
        await message.reply("✅ 发送目标已设置！")
        return True
    await dialogue.prompt(message, "❌ 保存失败，请稍后再试。")
    return False


async def _handle_setrename(message, uid) -> bool:
    tag = (message.text or "").strip()
    if await set_field(uid, "rename_tag", tag):
        await message.reply(f"✅ 文件名标签已设置为：`{tag}`")
        return True
    await dialogue.prompt(message, "❌ 保存失败，请稍后再试。")
    return False


async def _handle_setcaption(message, uid) -> bool:
    if await set_field(uid, "caption", message.text or ""):
        await message.reply("✅ 追加文案已保存！")
        return True
    await dialogue.prompt(message, "❌ 保存失败，请稍后再试。")
    return False


async def _handle_setreplacement(message, uid) -> bool:
    match = re.fullmatch(r"'(.+)' '(.+)'", (message.text or "").strip())
    if not match:
        await dialogue.prompt(
            message, "❌ 格式不正确。正确格式：`'原词' '替换词'`，可重新发送或 /cancel 取消。"
        )
        return False
    word, replacement = match.groups()

    try:
        saved = await update_word_rules(uid, replacement=(word, replacement))
    except RuleConflict as exc:
        await dialogue.prompt(
            message, f"❌ 替换与删词规则冲突：`{exc}`。请恢复默认规则后重新设置。"
        )
        return False
    if saved:
        await message.reply(f"✅ 替换规则已保存：`{word}` → `{replacement}`")
        return True
    await dialogue.prompt(message, "❌ 保存失败，请稍后再试。")
    return False


async def _handle_deleteword(message, uid) -> bool:
    words = (message.text or "").split()
    if not words:
        await dialogue.prompt(message, "❌ 未识别到要删除的词。")
        return False
    try:
        saved = await update_word_rules(uid, delete_words=words)
    except RuleConflict as exc:
        await dialogue.prompt(
            message, f"❌ 删词与替换规则冲突：`{exc}`。请恢复默认规则后重新设置。"
        )
        return False
    if saved:
        await message.reply(f"✅ 已添加删词：`{'、'.join(words)}`")
        return True
    await dialogue.prompt(message, "❌ 保存失败，请稍后再试。")
    return False


async def _handle_setthumb(message, uid) -> None:
    if not message.photo:
        await dialogue.prompt(message, "❌ 请发送一张图片。")
        return

    st = state.get(uid)
    temp_path = None
    thumb_path = thumbnail_path(uid)
    try:
        temp_path = await asyncio.wait_for(message.download(), timeout=60)
        if state.get(uid) is not st:
            return
        if not temp_path:
            await dialogue.prompt(message, "❌ 图片下载失败，请重试。")
            return
        normalize_thumbnail(temp_path, thumb_path)
        state.clear(uid)
        await message.reply("✅ 封面已更新！")
    except (TimeoutError, OSError, ValueError) as e:
        logger.warning("保存封面失败 user=%s type=%s", uid, type(e).__name__)
        if state.get(uid) is st:
            await dialogue.prompt(message, "❌ 图片下载超时或保存失败，请重试。")
    finally:
        if temp_path and os.path.abspath(temp_path) != os.path.abspath(thumb_path):
            with suppress(OSError):
                os.remove(temp_path)
