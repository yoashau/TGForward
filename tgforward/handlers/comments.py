"""评论按钮：后台提取文字与媒体附件，复用传输设置和任务取消。"""

import logging

from pyrogram import filters

from tgforward.runtime import lifecycle
from tgforward.storage.users import is_whitelisted
from tgforward.telegram import clients as clients_registry
from tgforward.telegram.clients import bot
from tgforward.ui.interaction import interaction

logger = logging.getLogger(__name__)


def _callback_filter(_, __, query):
    return isinstance(query.data, str) and query.data.startswith("cmt:")


async def _pick_session(uid: int, chat_ref: str):
    """选择抓取会话：点击者的登录会话优先；公开频道可降级 Premium 会话。"""
    session = await clients_registry.get_user_client(uid)
    if session is not None:
        return session
    is_private = str(chat_ref).startswith("-100") or str(chat_ref).lstrip("-").isdigit()
    if not is_private and clients_registry.premium_started and clients_registry.premium:
        return clients_registry.premium
    return None


@bot.on_callback_query(filters.create(_callback_filter))
@interaction
@lifecycle.serialized
async def on_fetch_comments(client, query):
    uid = query.from_user.id
    if not await is_whitelisted(uid):
        await query.answer("⚠️ 你没有使用权限。", show_alert=True)
        return

    try:
        parts = query.data.split(":")
        if len(parts) not in (3, 4):
            raise ValueError
        chat_ref, msg_id = parts[1], int(parts[2])
        owner = int(parts[3]) if len(parts) == 4 else uid
        if msg_id <= 0 or owner != uid:
            raise ValueError
    except (ValueError, TypeError):
        await query.answer("请使用你自己的任务管理消息中的评论按钮。", show_alert=True)
        return
    from tgforward.transfers.progress import is_task_result

    if not is_task_result(query.message) or (len(parts) == 3 and query.message.chat.id != uid):
        await query.answer("请重新提取原帖，再使用任务管理消息中的评论按钮。", show_alert=True)
        return

    from tgforward.comments import discussion
    from tgforward.comments.actions import CommentButton
    from tgforward.runtime import tasks
    from tgforward.storage.users import load_user_settings
    from tgforward.ui import state

    button = CommentButton(client or bot, query.message)
    active = tasks.get(uid)
    if active and (active.comment_target == button.key or active.kind == "comments"):
        await query.answer("评论正在提取中")
        return
    if state.is_busy(uid):
        await query.answer("请先完成当前输入，或发送 /cancel 退出。", show_alert=True)
        return
    session = await _pick_session(uid, chat_ref)
    if session is None:
        await query.answer(
            "🔐 请先在机器人私聊中发送 /login，登录能查看评论的账号。", show_alert=True
        )
        return
    try:
        task = tasks.register(uid, "comments", 1)
    except tasks.TaskAlreadyActive:
        await query.answer("已有提取任务，请等待完成，或发送 /cancel 取消。", show_alert=True)
        return
    except tasks.TaskCooldown as exc:
        await query.answer(f"请 {int(exc.remaining) + 1} 秒后重试。")
        return
    button.task = task
    task.media_scope = "本次评论"
    task.comment_target = button.key
    task.active_unit = task.extraction_unit(("comments", chat_ref, msg_id))
    task.active_unit.status = button
    button.unit = task.active_unit
    task.status = button
    try:
        await query.answer("提取评论会从头读取，已发送内容可能重复；可点击停止。")
        if task.cancel_requested:
            task.comment_target = None
            tasks.finish(uid, task)
            await button.finish(outcome="stopped")
            return
        await button.set_running()
    except BaseException:
        task.comment_target = None
        tasks.finish(uid, task)
        await button.finish(outcome="stopped")
        raise

    async def work():
        result = "stopped"
        summary = "评论提取已停止，可重新提取，已发送评论可能重复。"
        try:
            settings = await load_user_settings(uid)
            uploader = await clients_registry.get_upload_bot(uid)
            summary = await discussion.extract(
                session, uploader, chat_ref, msg_id, settings, uid, task, button
            )
            result = "partial" if task.comments_incomplete else "success"
            task.advance(success=True)
        except tasks.TaskCancelled:
            raise
        except Exception as exc:
            result = "failed"
            summary = f"评论提取失败：{str(exc)[:300]}"
            logger.exception("评论提取失败 task=%s", task.token)
        finally:
            task.comment_target = None
            if task.timed_out:
                summary = "⚠️ 评论提取长时间没有进度，已停止。"
            if task.cancel_reason != tasks.CancelReason.REVOKED:
                await button.finish(summary, outcome=result)
            task.status = None
            task.active_unit = None

    tasks.launch(task, work, button.edit)
    if task.runner is None:
        task.comment_target = None
        await button.finish(outcome="stopped")
    else:
        # 首次调度前取消时 work 的 finally 尚未运行，仍需恢复按钮。
        async def cleanup():
            await button.finish(outcome="stopped")

        def completed(_):
            if task.comment_target is not None:
                task.comment_target = None
                import asyncio

                asyncio.create_task(cleanup())

        task.runner.add_done_callback(completed)
