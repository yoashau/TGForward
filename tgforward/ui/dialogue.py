"""统一的对话提示与退出按钮；按钮绑定用户及本次操作，旧按钮不影响新操作。"""

from contextlib import suppress

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tgforward.runtime import tasks
from tgforward.ui import state
from tgforward.ui.i18n import tr


def cancel_keyboard(uid: int):
    st = state.get(uid)
    task = tasks.get(uid)
    token = st.token if st else task.token if task else None
    if token is None:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("↩️ 退出填写") if st else tr("⏹ 停止提取消息"),
                    callback_data=f"flow:cancel:{uid}:{token}",
                )
            ]
        ]
    )


async def clear(uid: int, *, keep=None) -> bool:
    st = state.get(uid)
    if st is None:
        return False
    state.clear(uid)
    if st.data.get("ui"):
        bucket = st.data["ui"]
        if keep is not None:
            bucket.items.pop((keep.chat.id, keep.id), None)
        await bucket.delete()
        if keep is not None:
            bucket.add(keep)
    temp = st.data.get("temp_client")
    if temp is not None:
        with suppress(Exception):
            from tgforward.telegram.clients import _stop_quietly

            await _stop_quietly(temp, "dialogue")
    return True


async def begin(message, kind: str, step: str, text: str, *, uid: int | None = None):
    uid = message.from_user.id if uid is None else uid
    panel = getattr(message, "_panel", None)
    await clear(uid, keep=panel.message if panel else None)
    st = state.set(uid, kind, step)
    if panel:
        st.data["panel"] = panel
    try:
        status = await prompt(message, text, uid=uid)
    except BaseException:
        if state.get(uid) is st:
            state.clear(uid)
        raise
    st.data["status_msg"] = status
    return st


async def prompt(message, text: str, *, uid: int | None = None):
    return await message.reply(
        text.rstrip()
        + (
            tr("\n\n👇 按上方提示发送；点「返回」可退出。")
            if getattr(message, "_panel", None)
            else tr("\n\n👇 请直接发送内容；点「退出填写」可取消。")
        ),
        reply_markup=cancel_keyboard(message.from_user.id if uid is None else uid),
    )


async def input_prompt(message, text, *, uid=None):
    return await prompt(message, text, uid=uid)


async def cancellable_notice(message, text, *, uid=None):
    return await message.reply(
        text, reply_markup=cancel_keyboard(message.from_user.id if uid is None else uid)
    )


async def processing_notice(message, text):
    panel = getattr(message, "_panel", None)
    if panel is not None:
        return await panel.render(
            message, text, navigation="none", revision=getattr(message, "_revision", None)
        )
    return await message.reply(text, reply_markup=None)


async def shutdown() -> None:
    for uid in list(state._states):
        await clear(uid)


def input_guard(kind):
    """在第一次 await 之前原子认领输入，不让另一 worker 重复消费同一表单。"""
    from functools import wraps

    def decorate(func):
        @wraps(func)
        async def wrapped(client, message, *args, **kwargs):
            st = state.get(message.from_user.id)
            if st is None or st.kind != kind:
                return
            if st.data.get("processing"):
                await processing_notice(message, tr("⏳ 上一次输入仍在处理，请稍候。"))
                return
            st.data["processing"] = True
            try:
                return await func(client, message, *args, **kwargs)
            finally:
                st.data["processing"] = False

        return wrapped

    return decorate


async def require_idle(message):
    if tasks.is_active(message.from_user.id):
        await message.reply(tr("⏳ 提取任务仍在运行，请等待结束，或 /cancel 后等任务停止再操作。"))
        return False
    return True
