import asyncio
from unittest.mock import AsyncMock

import pytest

from tgforward.runtime import lifecycle, tasks
from tgforward.ui.i18n import current_language, language_context


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(tasks, "_tasks", {})
    monkeypatch.setattr(tasks, "_queues", {})
    monkeypatch.setattr(tasks, "_last_finished", {})
    monkeypatch.setattr(tasks, "USER_COOLDOWN", 0)
    monkeypatch.setattr(lifecycle, "revoked", set())
    monkeypatch.setattr(lifecycle, "_generations", {})


async def drain(uid):
    while task := tasks.get(uid):
        await asyncio.wait_for(task.runner, 1)


def test_fifo_waits_for_cleanup_and_preserves_request_language():
    async def run():
        events = []
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def first(task):
            entered.set()
            try:
                await task.wait_or_cancel(3600)
            finally:
                cleaning.set()
                await release.wait()
                events.append("cleaned")

        async def later(task):
            events.append((task.total, current_language()))

        tasks.submit(1, "batch", 1, first, AsyncMock())
        await entered.wait()
        original = tasks.get(1)
        with language_context("en"):
            _, position = tasks.submit(1, "batch", 2, later, AsyncMock())
            assert position == 1
        with language_context("zh"):
            tasks.submit(1, "batch", 3, later, AsyncMock())
        tasks.request_cancel(1)
        await cleaning.wait()
        assert tasks.get(1) is original and events == []
        release.set()
        await drain(1)
        assert events == ["cleaned", (2, "en"), (3, "zh")]
        assert not tasks._queues

    asyncio.run(run())


def test_full_queue_has_no_waiting_coroutines_and_rejects_without_dropping_requests():
    async def run():
        active = tasks.register(1, "comments", 1)
        before = asyncio.all_tasks()
        work, notify = AsyncMock(), AsyncMock()
        requests = [tasks.submit(1, "batch", 1, work, notify)[0] for _ in range(9999)]
        assert asyncio.all_tasks() == before
        assert tasks.queued_count(1) == 9999
        with pytest.raises(tasks.QueueFull):
            tasks.submit(1, "batch", 1, AsyncMock(), AsyncMock())
        assert tasks.is_queued(1, requests[0].token)
        assert tasks.is_queued(1, requests[-1].token)
        assert tasks.clear_queue(1) == 9999
        assert tasks.get(1) is active and not active.cancelled
        tasks.finish(1, active)

    asyncio.run(run())


def test_remove_middle_request_and_isolate_users():
    async def run():
        original = tasks.register(1, "comments", 1)
        delivered = []

        async def work(task):
            delivered.append((task.user_id, task.total))

        first, _ = tasks.submit(1, "batch", 1, work, AsyncMock())
        middle, _ = tasks.submit(1, "batch", 2, work, AsyncMock())
        tasks.submit(1, "batch", 3, work, AsyncMock())
        tasks.submit(2, "batch", 4, work, AsyncMock())
        await drain(2)
        assert delivered == [(2, 4)]
        assert not tasks.remove_queued(2, middle.token)
        assert tasks.remove_queued(1, middle.token)
        assert not tasks.remove_queued(1, middle.token)
        tasks.finish(1, original)
        assert tasks.get(1).token == first.token
        tasks.finish(1, original)  # 旧 runner 的完成回调不能释放队首。
        await drain(1)
        assert delivered == [(2, 4), (1, 1), (1, 3)]

    asyncio.run(run())


@pytest.mark.parametrize("failure", [ValueError("failed"), tasks.TaskCancelled()])
def test_failed_or_cancelled_request_continues_queue(failure):
    async def run():
        next_work = AsyncMock()
        tasks.submit(1, "batch", 1, AsyncMock(side_effect=failure), AsyncMock())
        tasks.submit(1, "batch", 2, next_work, AsyncMock())
        await drain(1)
        next_work.assert_awaited_once()

    asyncio.run(run())


def test_stalled_request_continues_queue(monkeypatch):
    monkeypatch.setattr(tasks, "TASK_STALL_TIMEOUT", 0.02)

    async def run():
        async def stalled(task):
            await asyncio.Event().wait()

        tasks.submit(1, "batch", 1, stalled, AsyncMock())
        first = tasks.get(1)
        next_work = AsyncMock()
        tasks.submit(1, "batch", 2, next_work, AsyncMock())
        await drain(1)
        assert first.timed_out
        next_work.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("reason", [tasks.CancelReason.REVOKED, tasks.CancelReason.SHUTDOWN])
def test_lifecycle_cancel_discards_waiting_requests(reason):
    async def run():
        tasks.submit(1, "batch", 1, AsyncMock(), AsyncMock())
        waiting = AsyncMock()
        tasks.submit(1, "batch", 2, waiting, AsyncMock())
        if reason == tasks.CancelReason.SHUTDOWN:
            await tasks.shutdown()
            with pytest.raises(lifecycle.StalePermit):
                tasks.submit(1, "batch", 3, waiting, AsyncMock())
        else:
            lifecycle.revoke(1)
            tasks.request_cancel(1, reason)
            runner = tasks.get(1).runner
            await asyncio.gather(runner, return_exceptions=True)
        assert not tasks.is_active(1) and not tasks._queues
        waiting.assert_not_awaited()

    asyncio.run(run())


def test_reallow_does_not_revive_queued_permit():
    async def run():
        first = tasks.register(1, "comments", 1)
        waiting = AsyncMock()
        tasks.submit(1, "batch", 2, waiting, AsyncMock())
        lifecycle.revoke(1)
        lifecycle.activate(1)
        tasks.finish(1, first)
        assert not tasks.is_active(1) and not tasks._queues
        waiting.assert_not_awaited()

    asyncio.run(run())


def test_cooldown_defers_instead_of_rejecting_and_remains_cancellable(monkeypatch):
    monkeypatch.setattr(tasks, "USER_COOLDOWN", 3600)

    async def run():
        first = tasks.register(1, "comments", 1)
        tasks.finish(1, first)
        work = AsyncMock()
        tasks.submit(1, "batch", 1, work, AsyncMock())
        await asyncio.sleep(0)
        work.assert_not_awaited()
        tasks.request_cancel(1)
        await drain(1)
        work.assert_not_awaited()

    asyncio.run(run())
