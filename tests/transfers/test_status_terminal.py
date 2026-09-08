import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from tgforward.runtime import lifecycle
from tgforward.runtime.tasks import Task
from tgforward.transfers.progress import TaskStatus


def test_terminal_render_retry_and_conflicting_outcome():
    async def run():
        message = NS(edit=AsyncMock(side_effect=[OSError("disconnected"), None]))
        status = TaskStatus(message, Task(981, "single", 1))
        with pytest.raises(OSError):
            await status.finish("done", "success")
        assert status.terminal_outcome == "success" and not status.terminal_rendered
        await status.edit("late")
        await status.finish("failed", "failed")
        assert message.edit.await_count == 1
        await status.finish("done", "success")
        await status.finish("done", "success")
        assert status.terminal_rendered and message.edit.await_count == 2

    asyncio.run(run())


def test_running_edit_cannot_overtake_terminal_render():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        rendered = []

        async def edit(text, **kwargs):
            if text.startswith("running"):
                entered.set()
                await release.wait()
            rendered.append(text)

        status = TaskStatus(NS(edit=edit), Task(981, "single", 1))
        running = asyncio.create_task(status.edit("running"))
        await entered.wait()
        terminal = asyncio.create_task(status.finish("done"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(running, terminal)
        await status.edit("late")
        assert len(rendered) == 2 and rendered[-1].startswith("done")

    asyncio.run(run())


def test_concurrent_finish_claims_one_outcome():
    async def run():
        message = NS(edit=AsyncMock())
        status = TaskStatus(message, Task(981, "single", 1))
        await asyncio.gather(status.finish("done"), status.finish("error", "failed"))
        assert status.terminal_outcome == "success" and status.terminal_rendered
        message.edit.assert_awaited_once()

    asyncio.run(run())


def test_revoked_terminal_is_local_and_never_rendered(monkeypatch):
    monkeypatch.setattr(lifecycle, "revoked", set())

    async def run():
        task = Task(981, "single", 1)
        task.lifecycle_permit = lifecycle.capture_permit(981)
        status = TaskStatus(NS(edit=AsyncMock()), task)
        lifecycle.revoke(981)
        await status.finish("stopped", "stopped")
        lifecycle.activate(981)
        await status.finish("stopped", "stopped")
        await status.edit("late")
        assert status.terminal_outcome == "stopped" and not status.terminal_rendered
        status.message.edit.assert_not_awaited()

    asyncio.run(run())


def test_comment_terminal_failure_can_retry_without_changing_claim():
    from tgforward.comments.actions import CommentButton

    async def run():
        client = NS(edit_message_text=AsyncMock(side_effect=[OSError("disconnected"), None]))
        status = CommentButton(
            client, NS(id=4, chat=NS(id=981), text="original"), task=Task(981, "comments", 1)
        )
        await status.finish("done", "success")
        assert status.terminal_outcome == "success" and not status.terminal_rendered
        await status.finish("error", "failed")
        await status.set_running()
        await status.edit("late")
        assert client.edit_message_text.await_count == 1
        await status.finish("done", "success")
        await status.finish("done", "success")
        assert status.terminal_rendered and client.edit_message_text.await_count == 2

    asyncio.run(run())


def test_unchanged_terminal_response_is_confirmed_rendered():
    from pyrogram.errors import MessageNotModified

    async def run():
        status = TaskStatus(
            NS(edit=AsyncMock(side_effect=MessageNotModified())), Task(981, "single", 1)
        )
        await status.finish("done")
        await status.finish("done")
        assert status.terminal_rendered
        status.message.edit.assert_awaited_once()

    asyncio.run(run())


def test_terminal_reads_same_delivery_outcome_as_persistence():
    from tgforward.transfers.results import DeliveryPart

    async def run():
        task = Task(981, "single", 1)
        task.active_unit = task.extraction_unit("input")
        result = task.active_unit.message((5, 1))
        result.resolve_source([1], [DeliveryPart("media:1", 1)])
        result.delivery.begin_attempt("media:1")
        result.delivery.mark_uncertain("media:1")
        message = NS(edit=AsyncMock())
        status = TaskStatus(message, task)
        await status.finish("exception", "failed")
        assert status.terminal_outcome == result.outcome == "uncertain"
        assert "无法确认" in message.edit.call_args.args[0]
        assert "重复" in message.edit.call_args.args[0]

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["single", "batch"])
@pytest.mark.parametrize("reason", ["TIMEOUT", "SHUTDOWN", "REVOKED"])
def test_force_cancel_finishes_unit_before_detaching(monkeypatch, kind, reason):
    from tgforward.runtime.tasks import CancelReason
    from tgforward.transfers import extractor
    from tgforward.utils.links import MessageLink

    async def run():
        task = Task(981, kind, 2 if kind == "batch" else 1)
        task.set_cancel_reason(CancelReason[reason])
        status_message = NS(id=1, chat=NS(id=981), edit=AsyncMock())
        message = NS(from_user=NS(id=981), reply=AsyncMock(return_value=status_message))
        monkeypatch.setattr(
            extractor.clients, "get_user_client", AsyncMock(side_effect=asyncio.CancelledError)
        )
        ref = MessageLink("source", 1, False)
        with pytest.raises(asyncio.CancelledError):
            if kind == "batch":
                await extractor.extract_range(message, ref, 2, task)
            else:
                await extractor.extract_single(message, ref, task)
        assert task.status is None and task.active_unit is None
        status = task.units[0].status
        assert status.terminal_outcome == "stopped"
        if reason == "REVOKED":
            status_message.edit.assert_not_awaited()
        else:
            status_message.edit.assert_awaited_once()
            if reason == "SHUTDOWN":
                assert status_message.edit.call_args.kwargs["reply_markup"] is None

    asyncio.run(run())


def test_unvisited_batch_requests_prevent_terminal_failure():
    from tgforward.transfers.results import DeliveryPart

    async def run():
        task = Task(981, "batch", 2)
        task.active_unit = task.extraction_unit("input", 2)
        result = task.active_unit.message((5, 1))
        result.resolve_source([1], [DeliveryPart("media:1", 1)])
        result.delivery.begin_attempt("media:1")
        result.delivery.reject("media:1", "server rejected")
        result.delivery.finalize_failed("media:1")
        task.advance(success=False)
        status = TaskStatus(NS(edit=AsyncMock()), task)
        await status.finish("stopped", "stopped")
        assert status.terminal_outcome == "incomplete"

    asyncio.run(run())


def test_detached_unit_cannot_render_into_next_unit():
    async def run():
        task = Task(981, "batch", 2)
        first = task.extraction_unit("first")
        task.active_unit = first
        status = TaskStatus(NS(edit=AsyncMock()), task)
        task.active_unit = task.extraction_unit("second")
        await status.edit("late progress")
        await status.finish("late result", "stopped")
        status.message.edit.assert_not_awaited()
        assert status.terminal_outcome == "stopped"

    asyncio.run(run())
