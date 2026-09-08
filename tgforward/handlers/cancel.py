"""/cancel 和退出按钮：统一清理对话、断开临时会话、停止后台任务。"""

from pyrogram import filters

from tgforward.runtime import tasks
from tgforward.telegram.clients import bot
from tgforward.ui import dialogue, state
from tgforward.ui.interaction import interaction


async def _cancel(uid):
    cleared = []
    if await dialogue.clear(uid):
        cleared.append("填写操作")
    if tasks.request_cancel(uid):
        cleared.append("提取任务")
    return f"✅ 已取消：{'、'.join(cleared)}。" if cleared else "ℹ️ 当前没有进行中的操作。"


@bot.on_message(filters.command(["cancel", "cancle"]) & filters.private)
@interaction
async def cancel_command(client, message):
    await message.reply(await _cancel(message.from_user.id))


@bot.on_callback_query(filters.regex(r"^flow:result:"))
async def result_callback(client, query):
    # 结果按钮只确认状态，绝不取消用户后来启动的新任务。
    parts = query.data.split(":")
    if len(parts) != 4 or parts[2] != str(query.from_user.id):
        await query.answer("这是其他用户的提取结果。", show_alert=True)
        return
    await query.answer(
        "提取消息成功。" if parts[3] == "success" else "该次提取已结束，请查看结果说明。"
    )


@bot.on_callback_query(filters.regex(r"^flow:cancel:"))
@interaction
async def cancel_callback(client, query):
    try:
        _, _, uid, token = query.data.split(":")
        uid = int(uid)
    except (ValueError, TypeError):
        await query.answer("按钮无效。", show_alert=True)
        return
    if uid != query.from_user.id:
        await query.answer("这是其他用户的操作。", show_alert=True)
        return
    st, task = state.get(uid), tasks.get(uid)
    if token not in [x.token for x in (st, task) if x is not None]:
        await query.answer("该操作已结束，此按钮已过期。")
        return
    await query.answer("正在取消…")
    if st is not None and st.token == token:
        await dialogue.clear(uid)
        text = "✅ 已退出当前填写。"
    else:
        tasks.request_cancel(uid)
        if task is not None and task.kind == "comments":
            return  # 评论管理消息自行显示终态，不另发临时反馈。
        text = "✅ 已请求停止提取任务。"
    await query.message.reply(text)
