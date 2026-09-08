"""/login /logout —— 用户账号登录状态机（会话加密存储）。"""

import asyncio
import contextlib
import logging
import re

from pyrogram import Client, filters
from pyrogram.errors import (
    AuthKeyUnregistered,
    BadRequest,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    SessionExpired,
    SessionPasswordNeeded,
    SessionRevoked,
)
from pyrogram.types import Message

from tgforward.config import API_HASH, API_ID
from tgforward.handlers.common import ensure_whitelisted
from tgforward.runtime import lifecycle, tasks
from tgforward.storage.users import (
    clear_logout_pending,
    get_pending_logouts,
    get_session,
    record_logout_pending,
    remove_session,
    save_session,
)
from tgforward.telegram.clients import (
    _stop_quietly,
    bot,
    remove_user_client,
    safe_connect_client,
)
from tgforward.ui import dialogue, state
from tgforward.ui.interaction import interaction

logger = logging.getLogger(__name__)

STEP_PHONE = "phone"
STEP_CODE = "code"
STEP_PASSWORD = "password"


async def _edit(msg: Message, text: str) -> None:
    try:
        await msg.edit(text)
    except Exception as e:
        logger.debug("编辑消息失败：%s", e)


async def _abort_login(uid: int, status_msg: Message | None, text: str) -> None:
    """中断登录流程：断开临时客户端、清除状态、提示用户。"""
    st = state.get(uid)
    if st is not None:
        temp_client = st.data.get("temp_client")
        if temp_client is not None:
            with contextlib.suppress(Exception):
                await _stop_quietly(temp_client, "login")
    state.clear(uid)
    if status_msg is not None:
        await _edit(status_msg, text)


def _temp_client(uid: int) -> Client:
    return Client(
        f"login:{uid}",
        api_id=API_ID,
        api_hash=API_HASH,
        device_model="TGForward",
        in_memory=True,
    )


async def _save_login(uid, st, temp_client):
    async with lifecycle.user_lock(uid):
        if tasks.is_active(uid) or state.get(uid) is not st or uid in lifecycle.revoked:
            return False
        session_string = await temp_client.export_session_string()
        await _stop_quietly(temp_client, "login")
        if state.get(uid) is not st:
            return False
        return await save_session(uid, session_string)


# ─── /login ──────────────────────────────────────────────────────────────────


@bot.on_message(filters.command("login") & filters.private)
@interaction
@lifecycle.serialized
async def login_command(client, message):
    if not await dialogue.require_idle(message):
        return
    if not await ensure_whitelisted(message):
        return

    await dialogue.begin(
        message,
        "login",
        STEP_PHONE,
        "📱 **登录 Telegram 账号**\n\n请发送手机号（含国家区号）。\n例如：`+8613812345678`",
    )


@bot.on_message(state.login_active & filters.private & state.non_command)
@interaction
@lifecycle.serialized
async def handle_login_steps(client, message):
    if not await dialogue.require_idle(message):
        return
    uid = message.from_user.id
    st = state.get(uid)
    if st is None or st.kind != "login":
        return

    with contextlib.suppress(Exception):
        await message.delete()

    if st.data.get("processing"):
        await dialogue.prompt(message, "⏳ 上一次输入仍在验证，请稍候。")
        return
    status_msg = st.data.get("status_msg")

    if status_msg is None:
        status_msg = await message.reply("⏳ 处理中...")
        state.update(uid, status_msg=status_msg)

    text = (message.text or "").strip()
    st.data["processing"] = True

    try:
        if st.step == STEP_PHONE:
            if not re.fullmatch(r"\+[1-9]\d{6,14}", text):
                await dialogue.prompt(
                    message,
                    "❌ 手机号格式不正确，请以 `+` 开头，例如 `+8613812345678`。"
                    "当前仍在登录流程，链接不会被提取。",
                )
                return

            await _edit(status_msg, "⏳ 正在发送验证码...")
            temp_client = _temp_client(uid)
            st.data["temp_client"] = temp_client
            await asyncio.wait_for(safe_connect_client(temp_client), 30)
            sent_code = await asyncio.wait_for(temp_client.send_code(text), 30)
            if state.get(uid) is not st:
                await _stop_quietly(temp_client, "login")
                return
            state.update(
                uid,
                phone=text,
                phone_code_hash=sent_code.phone_code_hash,
                temp_client=temp_client,
            )
            state.set_step(uid, STEP_CODE)
            await dialogue.prompt(
                message,
                "📩 **输入验证码**\n\n"
                "请输入你收到的验证码，**数字之间用空格隔开**。\n"
                "示例：`1 2 3 4 5`",
            )

        elif st.step == STEP_CODE:
            temp_client = st.data["temp_client"]
            await _edit(status_msg, "⏳ 正在验证验证码...")
            try:
                code = text.replace(" ", "")
                await asyncio.wait_for(
                    temp_client.sign_in(st.data["phone"], st.data["phone_code_hash"], code), 30
                )
            except SessionPasswordNeeded:
                if state.get(uid) is not st:
                    return
                state.set_step(uid, STEP_PASSWORD)
                await dialogue.prompt(
                    message,
                    "🔒 **两步验证**\n\n请输入你的两步验证密码：",
                )
                return

            if state.get(uid) is not st:
                return
            if not await _save_login(uid, st, temp_client):
                if state.get(uid) is not st:
                    return
                await _abort_login(uid, status_msg, "❌ 会话保存失败，请重新 /login。")
                return
            state.clear(uid)
            await _edit(status_msg, "✅ **登录成功**\n\n现在可以提取账号有权访问的私有内容和评论。")

        elif st.step == STEP_PASSWORD:
            temp_client = st.data["temp_client"]
            await _edit(status_msg, "⏳ 正在验证密码...")
            await asyncio.wait_for(temp_client.check_password(text), 30)
            if state.get(uid) is not st:
                return
            if not await _save_login(uid, st, temp_client):
                if state.get(uid) is not st:
                    return
                await _abort_login(uid, status_msg, "❌ 会话保存失败，请重新 /login。")
                return
            state.clear(uid)
            await _edit(status_msg, "✅ **登录成功**\n\n现在可以提取账号有权访问的私有内容和评论。")

    except (PhoneCodeInvalid, PhoneCodeExpired):
        if state.get(uid) is not st:
            return
        await _abort_login(uid, status_msg, "❌ 验证码无效或已过期，请重新发送 /login 再试。")
    except BadRequest as e:
        if state.get(uid) is not st:
            return
        if st.step == STEP_PASSWORD:
            await dialogue.prompt(message, "❌ 密码验证失败，请重新输入密码。")
        else:
            await _abort_login(
                uid, status_msg, f"❌ 操作失败（{type(e).__name__}），请重新发送 /login 再试。"
            )
    except Exception as e:
        if state.get(uid) is not st:
            return
        logger.warning("登录流程出错 user=%s type=%s", uid, type(e).__name__)
        await _abort_login(uid, status_msg, "⚠️ 发生错误，请重新发送 /login 再试。")

    finally:
        st.data["processing"] = False
        if state.get(uid) is not st and st.data.get("temp_client") is not None:
            with contextlib.suppress(Exception):
                await _stop_quietly(st.data["temp_client"], "login-finally")


# ─── /logout ─────────────────────────────────────────────────────────────────


@bot.on_message(filters.command("logout") & filters.private)
@interaction
@lifecycle.serialized
async def logout_command(client, message):
    if not await dialogue.require_idle(message):
        return
    if not await ensure_whitelisted(message):
        return

    uid = message.from_user.id
    await dialogue.clear(uid)
    with contextlib.suppress(Exception):
        await message.delete()
    status_msg = await message.reply("⏳ 正在处理退出请求...")

    async with lifecycle.user_lock(uid):
        await _logout_locked(uid, status_msg)


async def _logout_locked(uid, status_msg):
    if tasks.is_active(uid):
        await _edit(status_msg, "⏳ 提取任务仍在运行，请等任务停止后重试。")
        return
    session_string = await get_session(uid)
    try:
        pending = await get_pending_logouts(uid)
    except Exception:
        await _edit(status_msg, "❌ 待重试记录读取失败，请稍后重新 /logout。")
        return
    sessions = list(dict.fromkeys(([session_string] if session_string else []) + pending))
    if not sessions:
        await _edit(status_msg, "❌ 未找到登录会话，你可能尚未登录。")
        return

    remote_failed = False
    tracking_failed = False
    for secret in sessions:
        temp_client = Client(
            f"logout:{uid}",
            api_id=API_ID,
            api_hash=API_HASH,
            device_model="TGForward",
            session_string=secret,
            in_memory=True,
        )
        try:
            await asyncio.wait_for(safe_connect_client(temp_client), 30)
            if await asyncio.wait_for(temp_client.log_out(), 30) is False:
                raise RuntimeError("Telegram returned false")
        except (AuthKeyUnregistered, SessionExpired, SessionRevoked):
            tracking_failed = not await clear_logout_pending(uid, secret) or tracking_failed
        except Exception as exc:
            remote_failed = True
            logger.warning("撤销 Telegram 会话失败 user=%s type=%s", uid, type(exc).__name__)
            if not await record_logout_pending(uid, secret, type(exc).__name__):
                await _edit(
                    status_msg, "❌ 远端撤销及重试记录保存失败，原凭据保留，请重新 /logout。"
                )
                return
        else:
            tracking_failed = not await clear_logout_pending(uid, secret) or tracking_failed
        finally:
            await _stop_quietly(temp_client, "logout")

    if session_string and not await remove_session(uid):
        await _edit(status_msg, "❌ 本地会话删除失败，请重试退出登录。")
        return
    await remove_user_client(uid)
    if remote_failed:
        await _edit(
            status_msg,
            "⚠️ 本地已退出，但远端 Session 撤销失败。已加密保存待撤销记录，"
            "可再次 /logout 重试，或在 Telegram「设置 → 设备」中结束该会话。",
        )
    elif tracking_failed:
        await _edit(
            status_msg, "⚠️ 本地已退出、远端授权已撤销，但重试记录清理失败，请重新 /logout。"
        )
    else:
        await _edit(status_msg, "✅ 已成功退出登录，远端授权已撤销！")
