import time

import pytest

from tgforward.runtime import tasks
from tgforward.runtime.tasks import TaskAlreadyActive, TaskCooldown


@pytest.fixture(autouse=True)
def _clean_registry():
    tasks._tasks.clear()
    tasks._last_finished.clear()
    yield
    tasks._tasks.clear()
    tasks._last_finished.clear()


class TestRegistry:
    def test_register_and_finish(self):
        t = tasks.register(1, "single", 1)
        assert tasks.is_active(1)
        assert tasks.get(1) is t
        tasks.finish(1)
        assert not tasks.is_active(1)
        assert tasks.get(1) is None

    def test_double_register_raises(self):
        tasks.register(1, "batch", 5)
        try:
            tasks.register(1, "single", 1)
            raise AssertionError("should raise")
        except TaskAlreadyActive:
            pass
        tasks.finish(1)

    def test_users_isolated(self):
        tasks.register(1, "single", 1)
        t2 = tasks.register(2, "batch", 3)
        assert tasks.get(2) is t2
        tasks.finish(1)
        tasks.finish(2)

    def test_all_active(self):
        tasks.register(1, "single", 1)
        tasks.register(2, "batch", 3)
        active = tasks.all_active()
        assert set(active) == {1, 2}
        tasks.finish(1)
        tasks.finish(2)
        assert tasks.all_active() == {}


class TestCancel:
    def test_request_cancel(self):
        t = tasks.register(1, "single", 1)
        assert tasks.request_cancel(1) is True
        assert t.cancelled is True
        tasks.finish(1)

    def test_cancel_unknown_user(self):
        assert tasks.request_cancel(999) is False


class TestCooldown:
    def test_cooldown_right_after_finish(self):
        tasks.register(1, "single", 1)
        tasks.finish(1)
        try:
            tasks.register(1, "single", 1)
            raise AssertionError("should raise TaskCooldown")
        except TaskCooldown as e:
            assert e.remaining > 0

    def test_cooldown_expires(self):
        tasks.register(1, "single", 1)
        tasks.finish(1)
        tasks._last_finished[1] = time.monotonic() - 100
        t = tasks.register(1, "single", 1)
        assert tasks.get(1) is t
        tasks.finish(1)

    def test_cooldown_not_shared_between_users(self):
        tasks.register(1, "single", 1)
        tasks.finish(1)
        t = tasks.register(2, "single", 1)
        assert tasks.get(2) is t
        tasks.finish(1)
        tasks.finish(2)

    def test_advance(self):
        t = tasks.register(1, "batch", 3)
        t.advance(success=True)
        t.advance()
        assert (t.current, t.success) == (2, 1)
        tasks.finish(1)
