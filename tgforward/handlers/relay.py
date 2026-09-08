"""辅助机器人绑定：询问 → 验证 → 保存；兼容带参数命令。"""

import asyncio
import contextlib
import re

from pyrogram import filters

from tgforward.config import BOT_TOKEN
from tgforward.handlers.common import ensure_whitelisted
from tgforward.runtime import lifecycle
from tgforward.storage.users import get_helper_token, remove_helper_token, save_helper_token
from tgforward.telegram.clients import (
    _client,
    _stop_quietly,
    bot,
    remove_helper_bot,
    safe_connect_client,
)
from tgforward.ui import dialogue, state
from tgforward.ui.interaction import interaction

_BOT_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")


@bot.on_message(filters.command("bindbot") & filters.private)
@interaction
@lifecycle.serialized
async def bind_bot(client, message):
    if not await dialogue.require_idle(message):
        return
    if not await ensure_whitelisted(message):
        return
    parts = message.command or []
    await dialogue.begin(
        message,
        "helper",
        "token",
        "🤖 **绑定辅助机器人**\n\n请发送 @BotFather 提供的 Bot Token。\n验证通过后才会替换原绑定。",
    )
    if len(parts) == 2:
        await _bind(message, parts[1].strip())


@bot.on_message(state.helper_active & filters.private & state.non_command)
@interaction
@lifecycle.serialized
async def bind_bot_input(client, message):
    if not await dialogue.require_idle(message):
        return
    if not await ensure_whitelisted(message):
        await dialogue.clear(message.from_user.id)
        return
    await _bind(message, (message.text or "").strip())


async def _bind(message, token):
    uid = message.from_user.id
    st = state.get(uid)
    if st is None or st.kind != "helper":
        return
    with contextlib.suppress(Exception):
        await message.delete()
    if st.data.get("processing"):
        await dialogue.prompt(message, "⏳ 正在验证上一条 Token，请稍候。")
        return
    if not _BOT_TOKEN_RE.fullmatch(token):
        await dialogue.prompt(
            message, "❌ Token 格式不正确，请重新发送 @BotFather 提供的完整 Token。"
        )
        return
    if token == BOT_TOKEN:
        await dialogue.prompt(message, "❌ 请绑定另一台辅助机器人，而不是当前主机器人。")
        return
    st.data["processing"] = True
    candidate = _client(f"validate-helper:{uid}", bot_token=token, no_updates=True)
    st.data["temp_client"] = candidate
    try:
        # 只连接并验证身份，不启动用于收消息的 dispatcher。
        await asyncio.wait_for(safe_connect_client(candidate), 30)
        me = await asyncio.wait_for(candidate.sign_in_bot(token), 30)
        if state.get(uid) is not st:
            return
        if not me.is_bot:
            await dialogue.prompt(message, "❌ 该凭据不是机器人 Token。")
            return
        async with lifecycle.user_lock(uid):
            if state.get(uid) is not st or uid in lifecycle.revoked:
                return
            if not await save_helper_token(uid, token):
                await dialogue.prompt(message, "❌ 保存失败，原绑定未替换，请重试。")
                return
            await remove_helper_bot(uid)
            state.clear(uid)
        await message.reply(
            f"✅ 辅助机器人 @{me.username or me.id} 已绑定。\n"
            "请先向它发送 /start，或把它加入发送目标群组。"
        )
    except Exception:
        # Telegram 异常可能带凭据，不记录或回显 Token。
        if state.get(uid) is st:
            await dialogue.prompt(
                message, "❌ Token 验证失败或连接超时，请检查 Token 后重试。原绑定未替换。"
            )
    finally:
        st.data["processing"] = False
        st.data.pop("temp_client", None)
        with contextlib.suppress(Exception):
            await _stop_quietly(candidate, "helper-validation")


@bot.on_message(filters.command("unbindbot") & filters.private)
@interaction
@lifecycle.serialized
async def unbind_bot(client, message):
    if not await dialogue.require_idle(message):
        return
    if not await ensure_whitelisted(message):
        return
    uid = message.from_user.id
    async with lifecycle.user_lock(uid):
        if state.get(uid) and state.get(uid).kind == "helper":
            await dialogue.clear(uid)
        await _unbind_locked(uid, message)


async def _unbind_locked(uid, message):
    if not await get_helper_token(uid):
        await message.reply("ℹ️ 当前未绑定辅助机器人，无需解绑。可使用 /bindbot 开始绑定。")
        return
    if await remove_helper_token(uid):
        await remove_helper_bot(uid)
        if state.get(uid) and state.get(uid).kind == "helper":
            await dialogue.clear(uid)
        await message.reply("✅ 辅助机器人已解绑。")
    else:
        await message.reply("❌ 操作失败，请稍后再试。")
