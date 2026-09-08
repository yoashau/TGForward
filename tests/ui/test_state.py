from types import SimpleNamespace

import pytest

from tgforward.ui import state


@pytest.fixture(autouse=True)
def _clean_states():
    for uid in list(state._states):
        state.clear(uid)
    yield
    for uid in list(state._states):
        state.clear(uid)


def _msg(uid):
    return SimpleNamespace(from_user=SimpleNamespace(id=uid))


class TestStateRegistry:
    def test_set_get_clear(self):
        state.set(1, "login", "phone", status_msg=None)
        st = state.get(1)
        assert st is not None
        assert st.kind == "login"
        assert st.step == "phone"

        state.set_step(1, "code")
        assert state.get(1).step == "code"

        state.update(1, phone="+8613800138000")
        assert state.get(1).data["phone"] == "+8613800138000"

        state.clear(1)
        assert state.get(1) is None

    def test_is_busy(self):
        assert not state.is_busy(1)
        state.set(1, "settings", "caption")
        assert state.is_busy(1)
        state.clear(1)
        assert not state.is_busy(1)

    def test_busyness_is_per_user(self):
        state.set(1, "login", "phone")
        assert not state.is_busy(2)
        state.clear(1)


class TestBusyFilter:
    def test_filter_with_state(self):
        state.set(1, "login", "phone")
        assert state._busy_filter(None, None, _msg(1)) is True
        assert state._busy_filter(None, None, _msg(2)) is False
        state.clear(1)

    def test_filter_without_from_user(self):
        msg = SimpleNamespace(from_user=None)
        assert state._busy_filter(None, None, msg) is False
