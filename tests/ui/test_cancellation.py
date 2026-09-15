import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from tgforward.handlers import cancel
from tgforward.runtime import tasks
from tgforward.transfers.progress import TaskStatus, stop_keyboard
from tgforward.ui import state
from tgforward.ui.i18n import tr


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(tasks, "_tasks", {})
    monkeypatch.setattr(tasks, "_queues", {})
    monkeypatch.setattr(tasks, "_last_finished", {})
    monkeypatch.setattr(state, "_states", {})


def query(task, data=None, uid=None):
    return NS(
        data=data or f"flow:cancel:{task.user_id}:{task.token}",
        from_user=NS(id=uid or task.user_id),
        message=task.status.message,
        answer=AsyncMock(),
    )


def active():
    task = tasks.register(1, "single", 1)
    message = NS(
        id=10, chat=NS(id=1), edit=AsyncMock(), edit_reply_markup=AsyncMock(), reply=AsyncMock()
    )
    task.status = TaskStatus(message, task)
    return task


def test_upload_confirmation_survives_progress_and_repeated_first_click():
    async def run():
        task = active()
        with task.upload_scope():
            first = query(task)
            await cancel.cancel_callback.__wrapped__(None, first)
            assert not task.cancelled
            first.answer.assert_awaited_once_with()
            assert "确认停止上传" in task.status.message.edit.call_args.args[0]
            markup = task.status.message.edit.call_args.kwargs["reply_markup"]
            confirm = markup.inline_keyboard[0][0].callback_data
            await task.status.edit("progress")
            assert task.status.message.edit.call_args.kwargs["reply_markup"] == markup
            assert "确认停止上传" in task.status.message.edit.call_args.args[0]
            await cancel.cancel_callback.__wrapped__(None, first)
            assert not task.cancelled
            await cancel.upload_cancel_callback.__wrapped__(None, query(task, confirm, uid=2))
            assert not task.cancelled
            await cancel.upload_cancel_callback.__wrapped__(None, query(task, confirm))
            assert task.cancelled

    asyncio.run(run())


def test_continue_invalidates_old_confirmation_and_next_upload_requires_fresh_confirmation():
    async def run():
        task = active()
        with task.upload_scope():
            await cancel.cancel_callback.__wrapped__(None, query(task))
            buttons = stop_keyboard(task).inline_keyboard[0]
            old = buttons[0].callback_data
            await task.status.edit("latest progress")
            timer = task._confirmation_runner
            await cancel.upload_cancel_callback.__wrapped__(
                None, query(task, buttons[1].callback_data)
            )
            assert task.cancel_confirmation is None and not task.cancelled
            assert task.status.message.edit.call_args.args[0].startswith("latest progress")
            await asyncio.gather(timer, return_exceptions=True)
            assert timer.cancelled() and task._confirmation_runner is None
            await cancel.upload_cancel_callback.__wrapped__(None, query(task, old))
            assert not task.cancelled
            await cancel.cancel_callback.__wrapped__(None, query(task))
            old = stop_keyboard(task).inline_keyboard[0][0].callback_data
        with task.upload_scope():
            await cancel.upload_cancel_callback.__wrapped__(None, query(task, old))
            assert not task.cancelled

    asyncio.run(run())


def test_download_cancels_once_and_old_button_cannot_stop_new_task():
    async def run():
        old = active()
        button = query(old)
        await cancel.cancel_callback.__wrapped__(None, button)
        assert old.cancelled
        tasks.finish(1, old)
        fresh = active()
        await cancel.cancel_callback.__wrapped__(None, button)
        assert not fresh.cancelled

    asyncio.run(run())


def test_cancel_command_requires_upload_confirmation():
    async def run():
        task = active()
        command = NS(from_user=NS(id=1), reply=AsyncMock())
        with task.upload_scope():
            await cancel.cancel_command.__wrapped__(None, command)
            assert not task.cancelled
            command.reply.assert_not_awaited()
            assert (
                task.status.message.edit.call_args.kwargs["reply_markup"]
                .inline_keyboard[0][0]
                .callback_data.startswith("flow:confirm:")
            )
        await cancel.cancel_command.__wrapped__(None, command)
        assert task.cancelled

    asyncio.run(run())


def test_queue_buttons_remove_only_owned_pending_requests():
    async def run():
        task = active()
        first, _ = tasks.submit(1, "batch", 1, AsyncMock(), AsyncMock())
        second, _ = tasks.submit(1, "batch", 1, AsyncMock(), AsyncMock())
        button = query(task, f"flow:queue:1:{first.token}", uid=2)
        await cancel.queue_callback.__wrapped__(None, button)
        assert tasks.queued_count(1) == 2
        button.from_user.id = 1
        await cancel.queue_callback.__wrapped__(None, button)
        assert tasks.queued_count(1) == 1 and not task.cancelled
        await cancel.queue_callback.__wrapped__(None, button)
        assert tasks.queued_count(1) == 1
        button.data = f"flow:clearqueue:1:{second.token}"
        await cancel.queue_callback.__wrapped__(None, button)
        assert not tasks.queued_count(1) and not task.cancelled

    asyncio.run(run())


def test_comment_progress_preserves_upload_confirmation_and_original_post_result():
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from tgforward.comments.actions import CommentButton

    async def run():
        task = tasks.register(1, "comments", 1)
        client = NS(edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("ok", callback_data="flow:result:1:success"),
                    InlineKeyboardButton("comments", callback_data="cmt:source:2:1"),
                ]
            ]
        )
        message = NS(id=3, chat=NS(id=1), text="post", entities=None, reply_markup=markup)
        task.status = CommentButton(client, message, task=task)
        with task.upload_scope():
            task.confirm_upload_cancel()
            await task.status.refresh_controls()
            await task.status.edit("progress")
            buttons = client.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard[0]
            assert buttons[0].callback_data == "flow:result:1:success"
            assert buttons[1].callback_data.startswith("flow:confirm:")
            assert buttons[2].callback_data.startswith("flow:continue:")

    asyncio.run(run())


@pytest.mark.parametrize("confirm", [False, True])
def test_upload_finishing_during_callback_keeps_terminal_buttons(confirm):
    async def run():
        task = active()
        status = task.status
        with task.upload_scope():
            button = query(task)
            handler = cancel.cancel_callback
            if confirm:
                task.confirm_upload_cancel()
                button.data = stop_keyboard(task).inline_keyboard[0][0].callback_data
                handler = cancel.upload_cancel_callback

            async def answer(*args, **kwargs):
                await status.finish("finished", "stopped" if task.cancelled else "success")
                task.status = None

            button.answer = answer
            await handler.__wrapped__(None, button)
            status.message.edit_reply_markup.assert_not_awaited()
            assert task._confirmation_runner is None
            assert (
                status.message.edit.call_args.kwargs["reply_markup"]
                .inline_keyboard[0][0]
                .callback_data.startswith("flow:result:")
            )

    asyncio.run(run())


def test_unconfirmed_stop_expires_and_restores_latest_progress(monkeypatch):
    monkeypatch.setattr(tasks, "UPLOAD_CANCEL_TIMEOUT", 0.02)

    async def run():
        task = active()
        with task.upload_scope():
            await task.status.edit("initial progress")
            await cancel.cancel_callback.__wrapped__(None, query(task))
            old = stop_keyboard(task).inline_keyboard[0][0].callback_data
            await task.status.edit("latest progress")
            assert "latest progress" not in task.status.message.edit.call_args.args[0]
            await asyncio.wait_for(task._confirmation_runner, 1)
            assert task.cancel_confirmation is None and not task.cancelled
            assert task._confirmation_runner is None
            assert task.status.message.edit.call_args.args[0].startswith("latest progress")
            assert (
                task.status.message.edit.call_args.kwargs["reply_markup"]
                .inline_keyboard[0][0]
                .callback_data.startswith("flow:cancel:")
            )
            await cancel.upload_cancel_callback.__wrapped__(None, query(task, old))
            assert not task.cancelled

    asyncio.run(run())


def test_leaving_upload_restores_progress_without_waiting_for_another_update():
    async def run():
        task = active()
        with task.upload_scope():
            await task.status.edit("latest progress")
            await cancel.cancel_callback.__wrapped__(None, query(task))
            timer = task._confirmation_runner
        await asyncio.wait_for(task._confirmation_runner, 1)
        assert timer.cancelled() and task.cancel_confirmation is None
        assert task.status.message.edit.call_args.args[0].startswith("latest progress")

    asyncio.run(run())


def test_expiry_cannot_overwrite_next_task():
    async def run():
        task = active()
        with task.upload_scope():
            await cancel.cancel_callback.__wrapped__(None, query(task))
            timer = task._confirmation_runner
            tasks.request_cancel(1)
            tasks.finish(1, task)
            fresh = active()
            await fresh.status.edit("next task progress")
            await asyncio.gather(timer, return_exceptions=True)
            assert timer.cancelled()
            fresh.status.message.edit.assert_awaited_once()
            assert not fresh.cancelled and fresh.cancel_confirmation is None

    asyncio.run(run())


def test_expired_confirmation_is_rejected_before_timer_runs():
    async def run():
        task = active()
        with task.upload_scope():
            await cancel.cancel_callback.__wrapped__(None, query(task))
            old = stop_keyboard(task).inline_keyboard[0][0].callback_data
            task.cancel_confirmation_deadline = 0
            await cancel.upload_cancel_callback.__wrapped__(None, query(task, old))
            assert not task.cancelled

    asyncio.run(run())


def test_comment_confirmation_expires_to_progress_and_keeps_post(monkeypatch):
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from tgforward.comments.actions import CommentButton

    monkeypatch.setattr(tasks, "UPLOAD_CANCEL_TIMEOUT", 0.02)

    async def run():
        task = tasks.register(1, "comments", 1)
        client = NS(edit_message_text=AsyncMock())
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("comments", callback_data="cmt:source:2:1")]]
        )
        message = NS(id=3, chat=NS(id=1), text="original post", entities=None, reply_markup=markup)
        task.status = CommentButton(client, message, task=task)
        with task.upload_scope():
            await cancel.cancel_callback.__wrapped__(None, query(task))
            await task.status.edit("latest comment progress")
            assert "确认停止上传" in client.edit_message_text.call_args.args[2]
            await asyncio.wait_for(task._confirmation_runner, 1)
            body = client.edit_message_text.call_args.args[2]
            assert "original post" in body and "latest comment progress" in body
            assert "确认停止上传" not in body and not task.cancelled
            button = client.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
            assert button.text == tr("⏹ 停止提取评论")

    asyncio.run(run())
