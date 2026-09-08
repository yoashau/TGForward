"""结束按钮、相册评论入口与菜单定位。"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pyrogram import enums, raw

from tgforward.comments import discussion
from tgforward.handlers import cancel, common, menu, start
from tgforward.runtime import tasks
from tgforward.storage.users import UserSettings
from tgforward.transfers import extractor
from tgforward.transfers.progress import TaskStatus
from tgforward.ui import interaction as ui
from tgforward.ui import panel
from tgforward.utils.links import MessageLink, find_links, parse_link


def test_comment_links_are_not_collapsed_to_channel_post():
    urls = [f"https://t.me/Cos_djtsl/17420?single&comment={i}" for i in [111424, 111444]]
    found = find_links("\n".join(urls))
    assert len(found) == 2
    assert [parse_link(u).comment_id for u in found] == [111424, 111444]
    assert parse_link(urls[0]).message_id == 17420


def test_album_comment_flag_is_read_from_leading_post():
    m = NS(id=17427, chat=NS(type=enums.ChatType.CHANNEL), media_group_id="album")
    c = NS(
        get_media_group=AsyncMock(return_value=[NS(id=i) for i in range(17420, 17428)]),
        resolve_peer=AsyncMock(
            return_value=raw.types.InputPeerChannel(channel_id=5, access_hash=6)
        ),
        invoke=AsyncMock(
            return_value=NS(
                messages=[
                    NS(id=17427, replies=None),
                    NS(id=17420, replies=NS(comments=True, replies=20)),
                ]
            )
        ),
    )
    assert asyncio.run(discussion.info(c, "Cos_djtsl", m)) == 20
    assert {x.id for x in c.invoke.call_args.args[0].id} == set(range(17420, 17428))


def test_collect_uses_discussion_root_not_first_returned_album_member(monkeypatch):
    group_id = 3018983021
    group = NS(id=group_id, megagroup=True, access_hash=99)
    raw_message = lambda mid, album=None: NS(  # noqa: E731
        id=mid,
        peer_id=raw.types.PeerChannel(channel_id=group_id),
        grouped_id=album,
    )
    mapping = NS(
        messages=[raw_message(111423, "album"), raw_message(111422, "album")],
        chats=[group],
        users=[],
    )

    async def invoke(request):
        if isinstance(request, raw.functions.channels.GetMessages):
            return NS(messages=[NS(id=17420, replies=NS(comments=True, channel_id=group_id))])
        if isinstance(request, raw.functions.messages.GetDiscussionMessage):
            assert request.msg_id == 17420
            return mapping
        assert isinstance(request, raw.functions.messages.GetReplies)
        assert request.peer.channel_id == group_id
        messages = (
            [raw_message(111444), raw_message(111424), *mapping.messages]
            if request.msg_id == 111422 and request.offset_id == 0
            else []
        )
        return NS(messages=messages, chats=[group], users=[])

    c = NS(
        get_messages=AsyncMock(return_value=NS(id=17427, media_group_id="album")),
        get_media_group=AsyncMock(return_value=[NS(id=i) for i in range(17420, 17428)]),
        # Reproduce the old library's messages[0] choice, without any network access.
        get_discussion_message=AsyncMock(return_value=NS(id=111423, chat=NS(id=-1003018983021))),
        resolve_peer=AsyncMock(
            return_value=raw.types.InputPeerChannel(channel_id=group_id, access_hash=99)
        ),
        invoke=AsyncMock(side_effect=invoke),
    )

    async def parse(client, message, *args, **kwargs):
        return NS(id=message.id, chat=NS(id=-1003018983021), empty=False)

    monkeypatch.setattr(discussion.Message, "_parse", parse)
    result, truncated = asyncio.run(discussion.collect(c, "Cos_djtsl", 17427))
    assert [m.id for m in result] == [111424, 111444]
    assert not truncated


def test_comment_link_reads_group_message_not_channel_post(monkeypatch):
    session = NS()
    monkeypatch.setattr(
        discussion, "resolve_root", AsyncMock(return_value=NS(chat_id=-1003018983021))
    )
    ref = parse_link("https://t.me/Cos_djtsl/17420?single&comment=111444")
    actual, reader = asyncio.run(extractor._resolve_comment_link(ref, session))
    assert actual == MessageLink("-1003018983021", 111444, True)
    assert reader is session


@pytest.mark.parametrize(
    "outcome,label",
    [
        ("success", "✅ 提取消息成功"),
        ("partial", "⚠️ 提取结束，部分未完成"),
        ("failed", "⚠️ 提取失败"),
        ("stopped", "⏹ 提取已停止"),
    ],
)
def test_terminal_button_is_not_cancel_and_late_progress_is_ignored(outcome, label):
    async def check():
        message = NS(edit=AsyncMock())
        status = TaskStatus(message, tasks.Task(1, "single", 1))
        await status.finish("result", outcome)
        await status.edit("late progress")
        message.edit.assert_awaited_once()
        button = message.edit.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
        assert button.text == label
        assert button.callback_data.startswith("flow:result:")

    asyncio.run(check())


def test_single_extraction_finishes_with_success_button(monkeypatch):
    source = NS(id=7, chat=NS(id=5), text="body")
    status = NS(edit=AsyncMock())
    request = NS(from_user=NS(id=1), chat=NS(id=1), reply=AsyncMock(return_value=status))
    for name in ["get_user_client", "get_upload_bot"]:
        monkeypatch.setattr(extractor.clients, name, AsyncMock(return_value=NS()))
    monkeypatch.setattr(extractor, "fetch_message", AsyncMock(return_value=source))
    monkeypatch.setattr(extractor, "load_user_settings", AsyncMock(return_value=UserSettings(1)))
    monkeypatch.setattr(extractor, "_comment_metadata", AsyncMock(return_value=None))
    monkeypatch.setattr(extractor, "_extract_comments", AsyncMock())
    for name in ["record_extract_success"]:
        monkeypatch.setattr(extractor, name, AsyncMock())
    monkeypatch.setattr(
        extractor.transfer, "transfer_message", AsyncMock(return_value=NS(summary="done"))
    )
    asyncio.run(
        extractor.extract_single(request, MessageLink("c", 7, False), tasks.Task(1, "single", 1))
    )
    assert (
        status.edit.call_args.kwargs["reply_markup"].inline_keyboard[0][0].text == "✅ 提取消息成功"
    )


def test_result_button_does_not_cancel_later_task(monkeypatch):
    request_cancel = AsyncMock()
    monkeypatch.setattr(tasks, "request_cancel", request_cancel)
    q = NS(data="flow:result:1:success", from_user=NS(id=1), answer=AsyncMock())
    asyncio.run(cancel.result_callback(None, q))
    q.answer.assert_awaited_once()
    request_cancel.assert_not_called()


def test_start_replaces_old_menu_but_navigation_edits_new_one(monkeypatch):
    monkeypatch.setattr(common, "is_whitelisted", AsyncMock(return_value=True))
    monkeypatch.setattr(menu, "is_whitelisted", AsyncMock(return_value=True))
    old = NS(id=1, chat=NS(id=1), delete=AsyncMock(), edit=AsyncMock())
    new = NS(id=11, chat=NS(id=1), delete=AsyncMock(), edit=AsyncMock())
    request = NS(
        id=10,
        chat=NS(id=1),
        from_user=NS(id=1),
        command=["start"],
        text="/start",
        delete=AsyncMock(),
        reply=AsyncMock(return_value=new),
    )

    async def check():
        panel._keyboards_removed.add(1)
        panel.acquire(1, old)
        await start.start_handler(None, request)
        old.delete.assert_awaited_once()
        request.reply.assert_awaited_once()
        assert panel.acquire(1).message is new
        q = NS(data="nav:account", from_user=NS(id=1), message=new, answer=AsyncMock())
        await menu.navigate(None, q)
        new.edit.assert_awaited_once()
        new.delete.assert_not_awaited()
        await ui.shutdown()

    asyncio.run(check())


def test_group_root_has_replies_without_channel_comments_flag():
    source = NS(id=111422, chat=NS(type=enums.ChatType.SUPERGROUP))
    c = NS(
        resolve_peer=AsyncMock(
            return_value=raw.types.InputPeerChannel(channel_id=3018983021, access_hash=99)
        ),
        invoke=AsyncMock(
            return_value=NS(messages=[NS(id=111422, replies=NS(comments=False, replies=20))])
        ),
    )
    assert asyncio.run(discussion.info(c, "-1003018983021", source)) == 20
    c.resolve_peer.assert_awaited_once_with(-1003018983021)


def comment_fixture(monkeypatch, chat_id=42, owner=42):
    from tgforward.handlers import comments
    from tgforward.transfers.progress import result_keyboard
    from tgforward.ui.keyboards import CommentAction

    monkeypatch.setattr(tasks, "_tasks", {})
    monkeypatch.setattr(tasks, "_last_finished", {})
    monkeypatch.setattr(comments, "is_whitelisted", AsyncMock(return_value=True))
    monkeypatch.setattr(comments, "_pick_session", AsyncMock(return_value=NS()))
    monkeypatch.setattr(comments.clients_registry, "get_upload_bot", AsyncMock(return_value=NS()))
    from tgforward.storage import users

    monkeypatch.setattr(users, "load_user_settings", AsyncMock(return_value=UserSettings(42)))
    from tgforward.ui import state

    monkeypatch.setattr(state, "is_busy", lambda _: False)
    markup = result_keyboard(
        tasks.Task(owner, "single", 1), "success", CommentAction("source", 7, 8)
    )
    message = NS(id=101, chat=NS(id=chat_id), reply_markup=markup, reply=AsyncMock())
    query = NS(
        from_user=NS(id=42),
        message=message,
        data=markup.inline_keyboard[0][-1].callback_data,
        answer=AsyncMock(),
    )
    client = NS(edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
    # Handler body only; interaction ownership and cleanup have separate integration tests.
    handler = comments.on_fetch_comments.__wrapped__.__wrapped__
    return handler, client, query


@pytest.mark.parametrize("chat_id", [42, -100123])
def test_comment_button_updates_in_place_and_repeated_click_is_not_a_second_task(
    monkeypatch, chat_id
):
    handler, client, query = comment_fixture(monkeypatch, chat_id)

    async def check():
        gate = asyncio.Event()

        async def extract(*args):
            await gate.wait()
            await args[-1].edit("download progress")
            return "done"

        sending = AsyncMock(side_effect=extract)
        monkeypatch.setattr(discussion, "extract", sending)
        await handler(client, query)
        task = tasks.get(42)
        assert task.comment_target == (chat_id, 101)
        await handler(client, query)
        assert query.answer.call_args.args[0] == "评论正在提取中"
        gate.set()
        await task.runner
        assert sending.await_count == 1
        query.message.reply.assert_not_awaited()
        calls = client.edit_message_text.call_args_list
        assert all(call.args[:2] == (chat_id, 101) for call in calls)
        labels = [call.kwargs["reply_markup"].inline_keyboard[0][-1].text for call in calls]
        assert labels[0] == "⏹ 停止提取评论" and labels[-1] == "✅ 评论已提取"
        assert not tasks.is_active(42)

    asyncio.run(check())


def test_comment_button_rejects_another_users_group_result(monkeypatch):
    handler, client, query = comment_fixture(monkeypatch, -100123, owner=99)
    asyncio.run(handler(client, query))
    client.edit_message_reply_markup.assert_not_awaited()
    assert "自己" in query.answer.call_args.args[0]
    assert not tasks.is_active(42)


@pytest.mark.parametrize("result", ["failed", "partial", "stopped"])
def test_comment_button_terminal_states_allow_retry_without_new_status_messages(
    monkeypatch, result
):
    handler, client, query = comment_fixture(monkeypatch)

    async def extract(*args):
        if result == "stopped":
            raise tasks.TaskCancelled()
        if result == "failed":
            raise extractor.TransferError("read failed")
        from tgforward.transfers.results import CommentResult

        args[-2].comment_result = CommentResult(failed=1)
        return "partial"

    monkeypatch.setattr(discussion, "extract", extract)

    async def check():
        await handler(client, query)
        task = tasks.get(42)
        await task.runner
        from tgforward.comments.actions import LABELS

        actual = client.edit_message_text.call_args.kwargs["reply_markup"]
        assert actual.inline_keyboard[0][-1].text == LABELS[result]
        query.message.reply.assert_not_awaited()
        assert not tasks.is_active(42)

    asyncio.run(check())


@pytest.mark.parametrize("automatic", [False, True])
def test_auto_comments_only_use_task_status(monkeypatch, automatic):
    from tgforward.ui.keyboards import CommentAction

    main = NS(edit_message_reply_markup=AsyncMock(), send_message=AsyncMock())
    monkeypatch.setattr(extractor.clients, "bot", main)
    monkeypatch.setattr(extractor.clients, "get_user_client", AsyncMock(return_value=NS()))
    sending = AsyncMock(return_value="done")
    monkeypatch.setattr(discussion, "extract", sending)
    request = NS(chat=NS(id=42), reply=AsyncMock())
    status = NS(edit=AsyncMock())
    asyncio.run(
        extractor._extract_comments(
            request,
            MessageLink("source", 8, False),
            UserSettings(42, auto_comments=automatic),
            main,
            tasks.Task(42, "single", 1),
            status,
            CommentAction("source", 7, 8),
        )
    )
    assert sending.await_count == int(automatic)
    if automatic:
        assert sending.call_args.args[2:4] == ("source", 7)
        assert sending.call_args.args[-1] is status
    main.edit_message_reply_markup.assert_not_awaited()
    main.send_message.assert_not_awaited()
    request.reply.assert_not_awaited()


def test_comment_state_preserves_message_result_button():
    from tgforward.comments.actions import CommentButton
    from tgforward.transfers.progress import result_keyboard
    from tgforward.ui.keyboards import CommentAction

    markup = result_keyboard(tasks.Task(42, "single", 1), "success", CommentAction("source", 7, 1))
    result_button = markup.inline_keyboard[0][0]
    client = NS(edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
    button = CommentButton(client, NS(id=80, chat=NS(id=42)), markup)
    asyncio.run(button.finish(outcome="running"))
    rows = client.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
    assert rows[0][0] is result_button
    assert rows[0][1].callback_data == "cmt:source:7:42"
