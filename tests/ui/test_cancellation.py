import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from tgforward.handlers import cancel
from tgforward.runtime import tasks
from tgforward.transfers.progress import TaskStatus, stop_keyboard
from tgforward.ui import state


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
            markup = task.status.message.edit_reply_markup.call_args.kwargs["reply_markup"]
            confirm = markup.inline_keyboard[0][0].callback_data
            await task.status.edit("progress")
            assert task.status.message.edit.call_args.kwargs["reply_markup"] == markup
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
            await cancel.upload_cancel_callback.__wrapped__(
                None, query(task, buttons[1].callback_data)
            )
            assert task.cancel_confirmation is None and not task.cancelled
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
            assert (
                command.reply.call_args.kwargs["reply_markup"]
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
            assert (
                status.message.edit.call_args.kwargs["reply_markup"]
                .inline_keyboard[0][0]
                .callback_data.startswith("flow:result:")
            )

    asyncio.run(run())
