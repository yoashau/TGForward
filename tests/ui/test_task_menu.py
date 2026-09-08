"""持久菜单与任务管理消息评论入口测试（虚拟 TTL，不等待真实时间）。"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pyrogram import enums

from tgforward.runtime import tasks
from tgforward.storage.users import UserSettings
from tgforward.transfers import extractor
from tgforward.ui import interaction as ui
from tgforward.ui import panel, state
from tgforward.utils.links import MessageLink


def message(mid=1):
    return NS(
        id=mid,
        chat=NS(id=42),
        from_user=NS(id=42),
        command=None,
        text="",
        edit=AsyncMock(),
        delete=AsyncMock(),
        reply=AsyncMock(),
    )


@pytest.mark.parametrize("elapsed", [30, 300])
def test_menu_survives_ttl_and_task_cleanup(monkeypatch, elapsed):
    async def check():
        menu, feedback, status = message(1), message(2), message(3)
        screen = panel.acquire(42, menu)
        bucket = ui.Messages()
        bucket.add(menu)
        bucket.add(feedback)
        bucket.add(status)
        gate, entered = asyncio.Event(), asyncio.Event()

        async def sleep(delay):
            assert delay == elapsed
            entered.set()
            await gate.wait()

        monkeypatch.setattr(ui.asyncio, "sleep", sleep)
        before = set(ui._pending)
        bucket.later(elapsed)
        await entered.wait()
        gate.set()
        await asyncio.gather(*(ui._pending - before))
        menu.delete.assert_not_awaited()
        feedback.delete.assert_awaited_once()
        status.delete.assert_awaited_once()
        assert screen.message is menu
        assert (42, 1) not in ui._owners
        await screen.close()
        menu.delete.assert_awaited_once()

    asyncio.run(check())


@pytest.mark.parametrize("data", ["nav:home", "set:mode", "flow:cancel:42:expired"])
def test_callbacks_never_temporarily_own_persistent_menu(monkeypatch, data):
    monkeypatch.setattr(state, "_states", {})

    async def check():
        menu = message()
        screen = panel.acquire(42, menu)
        # Telegram callback delivers another Message object with the same key.
        callback_message = message()
        query = NS(message=callback_message, from_user=NS(id=42), data=data, answer=AsyncMock())

        @ui.interaction
        async def callback(_, wrapped):
            assert not wrapped.message._messages.items
            assert (42, 1) not in ui._owners
            await wrapped.message._messages.delete()

        try:
            await callback(None, query)
            await ui.MessageView(callback_message, ui.Messages()).delete()
            menu.delete.assert_not_awaited()
            callback_message.delete.assert_not_awaited()
            assert screen.message is menu
        finally:
            await ui.shutdown()

    asyncio.run(check())


def test_new_menu_registered_before_reply_returns(monkeypatch):
    async def check():
        source, sent = message(1), message(2)
        source.reply.return_value = sent
        screen = panel.acquire(42)
        await screen.render(source, "menu")
        bucket = ui.Messages()
        bucket.add(sent)
        assert not bucket.items
        assert panel.is_persistent(sent)
        await screen.relocate()
        assert not panel.is_persistent(sent)
        assert screen.message is None
        sent.delete.assert_awaited_once()

    asyncio.run(check())


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("count", [None, 0, 12])
@pytest.mark.parametrize("album", [False, True])
def test_single_comment_entry_only_on_finished_management_message(
    monkeypatch, automatic, count, album
):
    source = NS(
        id=8, chat=NS(id=99, type=enums.ChatType.CHANNEL), media_group_id="a" if album else None
    )
    source.reply_markup = NS(inline_keyboard=[[NS(callback_data="source:button")]])
    main, helper, session = NS(send_message=AsyncMock()), NS(), NS()
    management, request = message(100), message(101)
    request.reply.return_value = management
    settings = UserSettings(42, auto_comments=automatic, chat_id="-100123/9")
    for name, value in [("get_user_client", session), ("get_upload_bot", helper)]:
        monkeypatch.setattr(extractor.clients, name, AsyncMock(return_value=value))
    monkeypatch.setattr(extractor.clients, "bot", main)
    monkeypatch.setattr(extractor, "load_user_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(extractor, "fetch_message", AsyncMock(return_value=source))
    monkeypatch.setattr(extractor.transfer, "_fetch_media_group", AsyncMock(return_value=[source]))
    info = AsyncMock(return_value=(7 if album else 8, count))
    monkeypatch.setattr(extractor.discussion, "post_info", info)
    automatic_extract = AsyncMock()
    monkeypatch.setattr(extractor.discussion, "extract", automatic_extract)
    for name in ["record_extract_success"]:
        monkeypatch.setattr(extractor, name, AsyncMock())

    async def transfer(*args, **kwargs):
        assert not any("markup" in key for key in kwargs)
        assert kwargs["status"].comment_action is None
        await kwargs["status"].edit("uploading")
        assert not any(
            str(b.callback_data).startswith("cmt:")
            for row in management.edit.call_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        )
        return NS(summary="完成：相册（8 项）" if album else "完成", sent_message=message(900))

    monkeypatch.setattr(extractor.transfer, "transfer_message", transfer)
    task = tasks.Task(42, "single", 1)
    asyncio.run(extractor.extract_single(request, MessageLink("channel", 8, False), task))
    rows = management.edit.call_args.kwargs["reply_markup"].inline_keyboard
    assert rows[0][0].text == "✅ 提取消息成功"
    assert len(rows) == 1 + int(automatic)
    assert len(rows[0]) == 2
    if automatic:
        assert rows[0][1].callback_data.startswith("flow:result:")
        assert rows[1][0].callback_data.startswith("cmt:")
    if not automatic:
        assert rows[0][1].text == (f"💬 提取评论（{count} 条）" if count else "💬 提取评论")
        assert rows[0][1].callback_data == f"cmt:channel:{7 if album else 8}:42"
    assert automatic_extract.await_count == int(automatic)
    if automatic:
        assert automatic_extract.call_args.args[3] == (7 if album else 8)
        assert automatic_extract.call_args.args[-1].message is management
    assert task.status is None
    main.send_message.assert_not_awaited()
    request.reply.assert_awaited_once()


@pytest.mark.parametrize("outcome", ["failed", "stopped", "partial"])
def test_incomplete_task_has_no_manual_comment_action(outcome):
    from tgforward.transfers.progress import result_keyboard
    from tgforward.ui.keyboards import CommentAction

    keyboard = result_keyboard(tasks.Task(42, "single", 1), outcome, CommentAction("c", 7, 12))
    assert len(keyboard.inline_keyboard[0]) == 1


@pytest.mark.parametrize("count", [None, 0, 12])
def test_generic_comment_builder_unknown_count_and_no_entry(count):
    from tgforward.transfers.progress import result_keyboard
    from tgforward.ui.keyboards import CommentAction

    task = tasks.Task(42, "single", 1)
    action = CommentAction("c", 7, count)
    button = result_keyboard(task, "success", action).inline_keyboard[0][1]
    assert button.text == (f"💬 提取评论（{count} 条）" if count else "💬 提取评论")
    absent = CommentAction("c", 7, count, has_comments=False)
    assert len(result_keyboard(task, "success", absent).inline_keyboard[0]) == 1


def test_comment_extraction_reclaims_management_ttl_until_finished(monkeypatch):
    from tests.ui.test_comment_actions import comment_fixture
    from tgforward.handlers import comments

    _, client, query = comment_fixture(monkeypatch)
    query.message.delete = AsyncMock()
    monkeypatch.setattr(state, "_states", {})

    async def check():
        original = ui.Messages()
        original.add(query.message)
        running, release = asyncio.Event(), asyncio.Event()

        async def extract(*args):
            running.set()
            await release.wait()

        monkeypatch.setattr(extractor.discussion, "extract", extract)
        try:
            await comments.on_fetch_comments(client, query)
            task = tasks.get(42)
            await running.wait()
            owner = ui._owners[(42, 101)]
            assert owner is not original and owner.deferred
            await comments.on_fetch_comments(client, query)
            assert ui._owners[(42, 101)] is owner
            # Simulate the original extraction's expired TTL while comments are running.
            await original.delete()
            query.message.delete.assert_not_awaited()
            release.set()
            await task.runner
            assert owner.items  # Retained for the normal feedback TTL after completion.
            await owner.delete()
            query.message.delete.assert_awaited_once()
        finally:
            release.set()
            await ui.shutdown()

    asyncio.run(check())


def test_legacy_media_comment_button_is_rejected(monkeypatch):
    from tests.ui.test_comment_actions import comment_fixture

    handler, client, query = comment_fixture(monkeypatch)
    query.message.reply_markup.inline_keyboard[0].pop(0)
    asyncio.run(handler(client, query))
    client.edit_message_reply_markup.assert_not_awaited()
    assert "任务管理消息" in query.answer.call_args.args[0]
    assert not tasks.is_active(42)


@pytest.mark.parametrize("album", [False, True])
@pytest.mark.parametrize("automatic", [False, True])
def test_batch_manual_entry_requires_one_unambiguous_original(monkeypatch, album, automatic):
    from tests.transfers.test_batches import _media_msg

    sources = [_media_msg(99, mid) for mid in (7, 8)]
    for source in sources:
        source.chat.type = enums.ChatType.CHANNEL
        source.media_group_id = "album" if album else None
    management, request = message(100), message(101)
    request.reply.return_value = management
    for name in ["get_user_client", "get_upload_bot"]:
        monkeypatch.setattr(extractor.clients, name, AsyncMock(return_value=NS()))
    monkeypatch.setattr(
        extractor,
        "load_user_settings",
        AsyncMock(return_value=UserSettings(42, auto_comments=automatic)),
    )
    monkeypatch.setattr(extractor, "fetch_message", AsyncMock(side_effect=sources))
    monkeypatch.setattr(extractor.transfer, "_fetch_media_group", AsyncMock(return_value=sources))
    monkeypatch.setattr(extractor.discussion, "post_info", AsyncMock(return_value=(7, 12)))
    comments = AsyncMock()
    monkeypatch.setattr(extractor.discussion, "extract", comments)
    for name in ["record_extract_success"]:
        monkeypatch.setattr(extractor, name, AsyncMock())
    monkeypatch.setattr(extractor, "BATCH_DELAY", 0)
    send = AsyncMock(return_value=NS(summary="done"))
    monkeypatch.setattr(extractor.transfer, "transfer_message", send)
    asyncio.run(
        extractor.extract_range(request, MessageLink("c", 7, False), 2, tasks.Task(42, "batch", 2))
    )
    buttons = management.edit.call_args.kwargs["reply_markup"].inline_keyboard[0]
    assert buttons[0].text == "✅ 提取消息成功"
    assert len(buttons) == 1 + int(album)
    if album and automatic:
        assert buttons[1].callback_data.startswith("flow:result:")
        assert (
            management.edit.call_args.kwargs["reply_markup"].inline_keyboard[1][0].text
            == "🔄 重新提取评论"
        )
    assert send.await_count == (1 if album else 2)
    assert comments.await_count == ((1 if album else 2) if automatic else 0)
    if album and not automatic:
        assert buttons[1].callback_data == "cmt:c:7:42"


def test_comment_link_does_not_offer_an_original_post_action(monkeypatch):
    from tests.transfers.test_batches import _media_msg

    source = _media_msg(-10099, 10)
    management, request = message(100), message(101)
    request.reply.return_value = management
    for name in ["get_user_client", "get_upload_bot"]:
        monkeypatch.setattr(extractor.clients, name, AsyncMock(return_value=NS()))
    monkeypatch.setattr(
        extractor,
        "_resolve_comment_link",
        AsyncMock(return_value=(MessageLink("-10099", 10, True), NS())),
    )
    monkeypatch.setattr(extractor, "fetch_message", AsyncMock(return_value=source))
    monkeypatch.setattr(extractor, "load_user_settings", AsyncMock(return_value=UserSettings(42)))
    info = AsyncMock(return_value=(10, 12))
    monkeypatch.setattr(extractor.discussion, "post_info", info)
    monkeypatch.setattr(
        extractor.transfer, "transfer_message", AsyncMock(return_value=NS(summary="done"))
    )
    for name in ["record_extract_success"]:
        monkeypatch.setattr(extractor, name, AsyncMock())
    ref = MessageLink("c", 7, False, comment_id=10)
    asyncio.run(extractor.extract_single(request, ref, tasks.Task(42, "single", 1)))
    info.assert_not_awaited()
    assert len(management.edit.call_args.kwargs["reply_markup"].inline_keyboard[0]) == 1


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize(
    "kind", [enums.ChatType.CHANNEL, enums.ChatType.SUPERGROUP, enums.ChatType.PRIVATE]
)
@pytest.mark.parametrize("read_error", [False, True])
def test_unknown_comment_metadata_keeps_channel_entry(monkeypatch, automatic, kind, read_error):
    from tgforward.ui.keyboards import comment_button

    info = AsyncMock(
        return_value=(7, None), side_effect=RuntimeError("raw unavailable") if read_error else None
    )
    monkeypatch.setattr(extractor.discussion, "post_info", info)
    source = NS(id=8, chat=NS(type=kind))
    action = asyncio.run(
        extractor._comment_metadata(
            MessageLink("channel", 8, False), source, UserSettings(42, auto_comments=automatic)
        )
    )
    if kind != enums.ChatType.CHANNEL:
        assert action is None
        return
    assert action is not None and action.has_comments
    assert action.comment_count is None
    assert action.post_id == (8 if read_error else 7)
    assert comment_button(action, 42).text == "💬 提取评论"
