import asyncio
from types import SimpleNamespace

import pytest

from tgforward.runtime import lifecycle
from tgforward.runtime.tasks import TaskCancelled
from tgforward.transfers.results import DeliveryPart, MessageDeliveryState, SideEffectRole


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(lifecycle, "revoked", set())
    monkeypatch.setattr(lifecycle, "_generations", {})
    monkeypatch.setattr(lifecycle, "_operation_locks", lifecycle.WeakValueDictionary())


def test_revoked_capture_and_same_generation_fence():
    permit = lifecycle.capture_permit(42)
    lifecycle.revoked.add(42)
    with pytest.raises(lifecycle.StalePermit):
        lifecycle.capture_permit(42)
    with pytest.raises(lifecycle.StalePermit):
        lifecycle.assert_current(permit)


def test_reallow_never_revives_old_permit_and_active_allow_is_idempotent():
    old = lifecycle.capture_permit(42)
    generation = lifecycle.revoke(42)
    assert lifecycle.validate_cleanup(42, generation)
    new = lifecycle.activate(42)
    assert new.generation > generation > old.generation
    assert lifecycle.activate(42) == new
    assert not lifecycle.validate_cleanup(42, generation)
    with pytest.raises(lifecycle.StalePermit):
        lifecycle.assert_current(old)
    lifecycle.assert_current(new)


def test_operation_serializes_allow_while_runner_wait_does_not_hold_user_lock():
    async def run():
        runner_ready, runner_done = asyncio.Event(), asyncio.Event()
        events = []

        async def runner():
            await runner_ready.wait()
            async with lifecycle.user_lock(42):
                events.append("runner exit")
            runner_done.set()

        async def ban():
            async with lifecycle.operation(42):
                generation = lifecycle.revoke(42)
                runner_ready.set()
                await runner_done.wait()
                await asyncio.sleep(0)
                async with lifecycle.user_lock(42):
                    assert lifecycle.validate_cleanup(42, generation)
                    events.append("cleanup")

        async def allow():
            await runner_ready.wait()
            async with lifecycle.operation(42):
                lifecycle.activate(42)
                events.append("allow")

        await asyncio.wait_for(asyncio.gather(runner(), ban(), allow()), 1)
        assert events == ["runner exit", "cleanup", "allow"]

    asyncio.run(run())


def test_authorization_checks_cancel_permit_role_and_every_part():
    state = MessageDeliveryState()
    state.seal([DeliveryPart("a", 1), DeliveryPart("b", 2)])
    task = SimpleNamespace(lifecycle_permit=lifecycle.capture_permit(42), check_cancel=lambda: None)
    role = SideEffectRole.FINAL_DELIVERY
    lifecycle.authorize_new_attempt(task, state, ["a", "b"], role)
    assert state.attempt_counts == {"a": 0, "b": 0}
    state.begin_attempt("b")
    state.mark_uncertain("b")
    with pytest.raises(ValueError):
        lifecycle.authorize_new_attempt(task, state, ["a", "b"], role)
    assert state.attempt_counts["a"] == 0
    with pytest.raises(ValueError):
        lifecycle.authorize_new_attempt(task, state, ["a"], SideEffectRole.STAGING_UPLOAD)
    lifecycle.revoke(42)
    with pytest.raises(lifecycle.StalePermit):
        lifecycle.authorize_new_attempt(task, state, ["a"], role)

    def cancelled():
        raise TaskCancelled()

    task.lifecycle_permit = lifecycle.activate(42)
    task.check_cancel = cancelled
    with pytest.raises(TaskCancelled):
        lifecycle.authorize_new_attempt(task, state, ["a"], role)
    assert state.attempt_counts["a"] == 0 and not state.confirmed_failed
