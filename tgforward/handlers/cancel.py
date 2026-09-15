"""/cancel 和退出按钮：统一清理对话、断开临时会话、停止后台任务。"""

from pyrogram import filters

from tgforward.runtime import tasks
from tgforward.telegram.clients import bot
from tgforward.transfers.progress import stop_keyboard
from tgforward.ui import dialogue, state
from tgforward.ui.i18n import tr
from tgforward.ui.interaction import interaction


async def _cancel(uid):
    cleared = []
    stopped = tasks.request_cancel(uid)
    if await dialogue.clear(uid):
        cleared.append(tr("填写操作"))
    if stopped:
        cleared.append(tr("提取任务"))
    return tr("✅ 已取消：{0}。", "、".join(cleared)) if cleared else tr("ℹ️ 当前没有进行中的操作。")


@bot.on_message(filters.command(["cancel", "cancle"]) & filters.private)
@interaction
async def cancel_command(client, message):
    task = tasks.get(message.from_user.id)
    if task is not None and task.confirm_upload_cancel():
        await message.reply(tr("tasks.confirm_upload_cancel"), reply_markup=stop_keyboard(task))
        return
    await message.reply(await _cancel(message.from_user.id))


@bot.on_callback_query(filters.regex(r"^flow:result:"))
async def result_callback(client, query):
    # 结果按钮只确认状态，绝不取消用户后来启动的新任务。
    parts = query.data.split(":")
    if len(parts) != 4 or parts[2] != str(query.from_user.id):
        await query.answer(tr("这是其他用户的提取结果。"), show_alert=True)
        return
    await query.answer(
        tr("提取消息成功。") if parts[3] == "success" else tr("该次提取已结束，请查看结果说明。")
    )


@bot.on_callback_query(filters.regex(r"^flow:cancel:"))
@interaction
async def cancel_callback(client, query):
    try:
        _, _, uid, token = query.data.split(":")
        uid = int(uid)
    except (ValueError, TypeError):
        await query.answer(tr("按钮无效。"), show_alert=True)
        return
    if uid != query.from_user.id:
        await query.answer(tr("这是其他用户的操作。"), show_alert=True)
        return
    st, task = state.get(uid), tasks.get(uid)
    if token not in [x.token for x in (st, task) if x is not None]:
        await query.answer(tr("该操作已结束，此按钮已过期。"))
        return
    if st is not None and st.token == token:
        await query.answer(tr("正在取消…"))
        await dialogue.clear(uid)
        text = tr("✅ 已退出当前填写。")
    else:
        if task.confirm_upload_cancel():
            status = task.status
            await query.answer(tr("tasks.confirm_upload_cancel"), show_alert=True)
            if status is not None:
                await status.refresh_controls()
            elif tasks.get(uid) is task and task.uploading and not task.cancelled:
                await query.message.edit_reply_markup(reply_markup=stop_keyboard(task))
            return
        tasks.request_cancel(uid)
        await query.answer(tr("正在取消…"))
        if task is not None and task.kind == "comments":
            return  # 评论管理消息自行显示终态，不另发临时反馈。
        text = tr("✅ 已请求停止提取任务。")
    await query.message.reply(text)


@bot.on_callback_query(filters.regex(r"^flow:(confirm|continue):"))
@interaction
async def upload_cancel_callback(client, query):
    try:
        _, action, uid, token, confirmation = query.data.split(":")
        uid = int(uid)
    except (ValueError, TypeError):
        await query.answer(tr("按钮无效。"), show_alert=True)
        return
    if uid != query.from_user.id:
        await query.answer(tr("这是其他用户的操作。"), show_alert=True)
        return
    task = tasks.get(uid)
    if (
        task is None
        or task.token != token
        or not task.uploading
        or task.cancelled
        or task.cancel_confirmation != confirmation
    ):
        await query.answer(tr("该操作已结束，此按钮已过期。"))
        return
    task.cancel_confirmation = None
    status = task.status
    is_status_message = status is not None and (query.message.chat.id, query.message.id) == (
        status.chat.id,
        status.id,
    )
    if action == "confirm":
        tasks.request_cancel(uid)
    await query.answer(tr("正在取消…") if action == "confirm" else tr("▶️ 继续上传"))
    if status is not None:
        await status.refresh_controls()
    # /cancel 的确认面板也需更新；不把已经结束的进度消息改回运行状态。
    if not is_status_message:
        await query.message.edit_reply_markup(reply_markup=None)


@bot.on_callback_query(filters.regex(r"^flow:(queue|clearqueue):"))
@interaction
async def queue_callback(client, query):
    try:
        _, action, uid, token = query.data.split(":")
        uid = int(uid)
    except (ValueError, TypeError):
        await query.answer(tr("按钮无效。"), show_alert=True)
        return
    if uid != query.from_user.id:
        await query.answer(tr("这是其他用户的操作。"), show_alert=True)
        return
    if not tasks.is_queued(uid, token):
        await query.answer(tr("该请求已开始或已移出队列。"))
        return
    if action == "clearqueue":
        text = tr("tasks.queue_cleared", tasks.clear_queue(uid))
    else:
        tasks.remove_queued(uid, token)
        text = tr("✅ 已移出队列。")
    await query.answer(text)
    await query.message.edit(text, reply_markup=None)
