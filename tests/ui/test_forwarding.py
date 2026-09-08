"""转发消息路由测试：来源解析与相册去重。"""

from types import SimpleNamespace

from tgforward.handlers.router import (
    _display_url,
    _group_recently_handled,
    _mark_group_handled,
    _ref_from_forward,
)
from tgforward.utils.links import MessageLink


def _fw(chat, mid=100):
    return SimpleNamespace(forward_from_chat=chat, forward_from_message_id=mid)


def test_public_channel_forward():
    chat = SimpleNamespace(username="channelname", id=-100111)
    ref = _ref_from_forward(_fw(chat, 100))
    assert ref == MessageLink("channelname", 100, False)


def test_private_channel_forward():
    chat = SimpleNamespace(username=None, id=-1001234567890)
    ref = _ref_from_forward(_fw(chat, 55))
    assert ref == MessageLink("-1001234567890", 55, True)


def test_basic_group_unsupported():
    chat = SimpleNamespace(username=None, id=-1234567)
    assert _ref_from_forward(_fw(chat)) is None


def test_no_forward_metadata():
    msg = SimpleNamespace(forward_from_chat=None, forward_from_message_id=None)
    assert _ref_from_forward(msg) is None


class TestDisplayUrl:
    def test_private(self):
        ref = MessageLink("-1001234567890", 42, True)
        assert _display_url(ref) == "https://t.me/c/1234567890/42"

    def test_public(self):
        ref = MessageLink("chan", 7, False)
        assert _display_url(ref) == "https://t.me/chan/7"


class TestGroupDedup:
    def test_mark_then_skip(self):
        _mark_group_handled(1, "g1")
        assert _group_recently_handled(1, "g1") is True
        assert _group_recently_handled(2, "g1") is False  # 用户隔离

    def test_no_gid_never_skips(self):
        assert _group_recently_handled(1, None) is False

    def test_expired_entry_ignored(self):
        import time as _t

        from tgforward.handlers import router

        router._recent_groups.setdefault(1, {})["old"] = _t.monotonic() - 999
        assert _group_recently_handled(1, "old") is False

    def teardown_method(self):
        from tgforward.handlers import router

        router._recent_groups.clear()
