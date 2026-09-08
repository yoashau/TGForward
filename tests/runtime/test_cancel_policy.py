import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pyrogram import StopTransmission

from tgforward.runtime import tasks
from tgforward.transfers.progress import make_progress
from tgforward.transfers.results import SideEffectRole


@pytest.mark.parametrize("first", list(tasks.CancelReason))
@pytest.mark.parametrize("second", list(tasks.CancelReason))
def test_cancel_reason_is_monotonic(first, second):
    task = tasks.Task(891, "single", 1)
    task.set_cancel_reason(first)
    task.set_cancel_reason(second)
    assert task.cancel_reason == max(first, second)
    assert task.cancelled and task.cancel_event.is_set()


def test_user_cancel_waits_for_inflight_work_and_interrupts_delay(monkeypatch):
    monkeypatch.setattr(tasks, "_tasks", {})
    monkeypatch.setattr(tasks, "_last_finished", {})

    async def run():
        task = tasks.register(891, "single", 1)
        started, release = asyncio.Event(), asyncio.Event()
        delivered = []

        async def work():
            started.set()
            await release.wait()
            delivered.append(1)
            await task.wait_or_cancel(3600)
            delivered.append(2)

        tasks.launch(task, work, AsyncMock())
        await started.wait()
        tasks.request_cancel(891)
        assert task.runner.cancelling() == 0
        release.set()
        await asyncio.wait_for(task.runner, 1)
        assert delivered == [1] and not tasks.is_active(891)

    asyncio.run(run())


def test_cancel_event_interrupts_long_wait():
    async def run():
        task = tasks.Task(891, "single", 1)
        waiter = asyncio.create_task(task.wait_or_cancel(3600))
        await asyncio.sleep(0)
        task.set_cancel_reason(tasks.CancelReason.USER)
        with pytest.raises(tasks.TaskCancelled):
            await asyncio.wait_for(waiter, 0.2)

    asyncio.run(run())


@pytest.mark.parametrize("role", list(SideEffectRole))
@pytest.mark.parametrize("reason", list(tasks.CancelReason))
def test_progress_cancellation_depends_on_side_effect_role(role, reason):
    async def run():
        task = tasks.Task(891, "single", 1)
        task.set_cancel_reason(reason)
        callback = make_progress(NS(), 1, 2, task, role=role)
        if role == SideEffectRole.FINAL_DELIVERY and reason == tasks.CancelReason.USER:
            await callback(0, 0)
        else:
            with pytest.raises(StopTransmission):
                await callback(0, 0)

    asyncio.run(run())


def test_send_authorizes_after_wait_and_rejects_cancel_before_rpc(monkeypatch):
    from tgforward.transfers import transfer

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        task = tasks.Task(891, "single", 1)
        rpc = AsyncMock()

        async def wait(*args):
            entered.set()
            await release.wait()

        monkeypatch.setattr(transfer, "heartbeat_sleep", wait)
        monkeypatch.setattr(transfer, "_send_lock", asyncio.Lock())
        sending = asyncio.create_task(transfer._send(rpc, task))
        await entered.wait()
        task.set_cancel_reason(tasks.CancelReason.USER)
        release.set()
        with pytest.raises(tasks.TaskCancelled):
            await sending
        rpc.assert_not_awaited()

    asyncio.run(run())


def test_revoke_interrupts_runner_before_delete_without_holding_user_lock(sqlite_store):
    from tgforward.runtime import lifecycle
    from tgforward.storage import users

    async def run():
        uid = 893
        await users.set_whitelisted(uid, True)
        started = asyncio.Event()
        events = []
        task = tasks.register(uid, "single", 1)

        async def work():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                async with lifecycle.user_lock(uid):
                    assert await users.get_user(uid) is not None
                    events.append("runner exited")

        notify = AsyncMock()
        tasks.launch(task, work, notify)
        await started.wait()
        assert await asyncio.wait_for(users.delete_user(uid), 1)
        assert events == ["runner exited"]
        assert task.cancel_reason == tasks.CancelReason.REVOKED
        assert await users.get_user(uid) is None
        assert not tasks.is_active(uid)
        notify.assert_not_awaited()

    asyncio.run(run())


def test_heartbeat_wait_responds_to_cancel_event():
    from tgforward.telegram.wait import heartbeat_sleep

    async def run():
        task = tasks.Task(891, "single", 1)
        waiter = asyncio.create_task(heartbeat_sleep(600, task))
        await asyncio.sleep(0)
        task.set_cancel_reason(tasks.CancelReason.USER)
        with pytest.raises(tasks.TaskCancelled):
            await asyncio.wait_for(waiter, 0.2)

    asyncio.run(run())


def test_cancelled_resource_wait_never_leaks_capacity():
    from tgforward.telegram.wait import acquire

    async def run():
        task = tasks.Task(891, "single", 1)
        resource = asyncio.Semaphore(0)
        entered = []

        async def work():
            async with acquire(resource, task):
                entered.append(True)

        waiter = asyncio.create_task(work())
        await asyncio.sleep(0)
        task.set_cancel_reason(tasks.CancelReason.USER)
        resource.release()
        with pytest.raises(tasks.TaskCancelled):
            await asyncio.wait_for(waiter, 0.2)
        assert not entered
        await asyncio.wait_for(resource.acquire(), 0.2)
        assert resource.locked()

    asyncio.run(run())


def test_delete_before_launch_clears_task_and_transient_state(sqlite_store):
    from tgforward.handlers import router
    from tgforward.storage import users
    from tgforward.ui import panel

    async def run():
        uid = 895
        await users.set_whitelisted(uid, True)
        task = tasks.register(uid, "single", 1)
        router._recent_groups[uid] = {"g": 999999999}
        screen = panel.acquire(uid)
        assert await users.delete_user(uid)
        assert not tasks.is_active(uid) and uid not in tasks._last_finished
        assert uid not in router._recent_groups and uid not in panel._panels and screen.closed
        assert task.cancel_reason == tasks.CancelReason.REVOKED
        await users.set_whitelisted(uid, True)
        fresh = tasks.register(uid, "single", 1)
        assert fresh.lifecycle_permit.generation > task.lifecycle_permit.generation
        tasks.finish(uid, fresh)
        tasks._last_finished.pop(uid, None)

    asyncio.run(run())
