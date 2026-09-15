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


def test_queued_forward_album_is_deduplicated_and_plan_is_preserved(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from tests.ui.test_interactions import msg
    from tgforward.handlers import router
    from tgforward.runtime import tasks

    monkeypatch.setattr(tasks, "_tasks", {})
    monkeypatch.setattr(tasks, "_last_finished", {})
    monkeypatch.setattr(tasks, "USER_COOLDOWN", 0)
    monkeypatch.setattr(router, "is_whitelisted", AsyncMock(return_value=True))
    monkeypatch.setattr(router, "_recent_groups", {})

    async def run():
        first = tasks.register(1, "comments", 1)
        single, batch = AsyncMock(), AsyncMock()
        monkeypatch.setattr(router, "extract_single", single)
        monkeypatch.setattr(router, "extract_range", batch)
        message = msg("https://t.me/channelname/200 3")
        message.forward_from_chat = SimpleNamespace(username="channelname", id=-100111)
        message.forward_from_message_id = 100
        message.media_group_id = "queued-album"
        await router.smart_router.__wrapped__(None, message)
        await router.smart_router.__wrapped__(None, message)
        assert tasks.queued_count(1) == 1 and message.reply.await_count == 1
        task_request = message.reply.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
        assert task_request.callback_data.startswith("flow:queue:1:")
        tasks.finish(1, first)
        task = tasks.get(1)
        # 多链接间隔本身已有取消测试，这里只验证排队后保留原计划。
        task.wait_or_cancel = AsyncMock()
        await task.runner
        assert single.call_args.args[1] == MessageLink("channelname", 100, False)
        assert batch.call_args.args[1:3] == (MessageLink("channelname", 200, False), 3)
        await router.shutdown()

    asyncio.run(run())
