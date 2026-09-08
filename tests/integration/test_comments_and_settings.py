"""菜单交互、评论附件与白名单分页。"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pyrogram import enums, raw


def run(coro):
    return asyncio.run(coro)


def message(mid=1, uid=1):
    return NS(
        id=mid,
        chat=NS(id=uid),
        from_user=NS(id=uid),
        text="",
        command=[],
        edit=AsyncMock(),
        delete=AsyncMock(),
        reply=AsyncMock(),
    )


@pytest.mark.parametrize("album", [False, True])
def test_private_inaccessible_copy_downloads(monkeypatch, album):
    from tests.transfers.test_routing import FakeDownloader, FakeUploader, _media_message
    from tgforward.storage.users import UserSettings
    from tgforward.transfers import transfer

    m = _media_message(photo=NS(file_size=100, file_id="p"))
    m.media_group_id = "album" if album else None
    up = FakeUploader()
    up.copy_message = AsyncMock(side_effect=ValueError("Can't copy this message"))
    up.copy_media_group = AsyncMock(
        side_effect=ValueError("Message with this type can't be copied.")
    )
    from tests.transfers.test_routing import confirmed_physical

    physical = AsyncMock(side_effect=confirmed_physical)
    monkeypatch.setattr(transfer, "_transfer_physical", physical)
    monkeypatch.setattr(transfer, "_send_album_physical", physical)
    run(
        transfer.transfer_message(
            up, FakeDownloader(group=[m]), m, UserSettings(1), "1", source_private=True
        )
    )
    assert physical.await_count == 1


def test_menu_has_only_key_first_level_actions():
    from tgforward.handlers.menu import page
    from tgforward.handlers.start import BOT_COMMANDS

    text, markup = page("home", 99999)
    actions = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert actions == ["nav:settings", "nav:account", "nav:close"]
    assert len(BOT_COMMANDS) == 3
    assert "欢迎使用" in text


@pytest.mark.parametrize(
    "action,kind", [("login", "login"), ("bindbot", "helper"), ("allow", "admin"), ("ban", "admin")]
)
def test_navigation_opens_dialogue_as_clicking_user(monkeypatch, action, kind):
    from tgforward.handlers import common, menu
    from tgforward.ui import interaction as ui
    from tgforward.ui import state

    monkeypatch.setattr(menu, "is_whitelisted", AsyncMock(return_value=True))
    monkeypatch.setattr(common, "is_whitelisted", AsyncMock(return_value=True))
    m = message(uid=1)
    m.from_user.id = 99  # 菜单消息作者是机器人，不是操作用户
    m.reply.return_value = message(2)
    q = NS(data=f"nav:do:{action}", from_user=NS(id=1), message=m, answer=AsyncMock())

    async def check():
        state._states.clear()
        await menu.navigate(None, q)
        assert state.get(1).kind == kind
        assert state.get(99) is None
        assert m.edit.await_count == 1
        m.reply.assert_not_awaited()
        await ui.shutdown()
        state._states.clear()

    run(check())


def test_cleanup_does_not_delete_final_result():
    from tgforward.ui.interaction import Messages, MessageView

    incoming, prompt, result = message(1), message(2), message(3)
    incoming.reply.return_value = prompt

    async def check():
        bucket = Messages()
        bucket.add(incoming)
        view = MessageView(incoming, bucket)
        await view.reply("progress")
        await bucket.delete()
        incoming.delete.assert_awaited_once()
        prompt.delete.assert_awaited_once()
        result.delete.assert_not_awaited()

    run(check())


def test_old_cleanup_cannot_delete_active_menu():
    from tgforward.ui.interaction import Messages

    m = message()

    async def check():
        old, new = Messages(), Messages()
        old.add(m)
        new.add(m)
        await old.delete()
        m.delete.assert_not_awaited()
        await new.delete()
        m.delete.assert_awaited_once()

    run(check())


def test_cancel_cleans_all_dialogue_inputs(monkeypatch):
    from tgforward.ui import dialogue, state
    from tgforward.ui import interaction as ui

    first, prompt, second, error = [message(i) for i in range(1, 5)]
    first.reply.return_value, second.reply.return_value = prompt, error

    @ui.interaction
    async def start(_, m):
        await dialogue.begin(m, "settings", "caption", "input")

    @ui.interaction
    async def invalid(_, m):
        await dialogue.prompt(m, "invalid")

    async def check():
        state._states.clear()
        await start(None, first)
        await invalid(None, second)
        await dialogue.clear(1)
        for m in [first, prompt, second, error]:
            m.delete.assert_awaited_once()
        await ui.shutdown()

    run(check())


def test_legacy_comment_callback_never_tracks_media_result():
    from tgforward.ui import interaction as ui

    m = message()
    q = NS(data="cmt:channel:1", message=m, from_user=NS(id=1), answer=AsyncMock())

    @ui.interaction
    async def callback(_, query):
        assert not query.message._messages.items

    async def check():
        await callback(None, q)
        await ui.shutdown()
        m.delete.assert_not_awaited()

    run(check())


def test_raw_comment_flag_detected_without_highlevel_replies():
    from tgforward.comments.discussion import info

    m = NS(id=42, chat=NS(type=enums.ChatType.CHANNEL))
    client = NS(
        resolve_peer=AsyncMock(return_value=NS(channel_id=7, access_hash=8)),
        invoke=AsyncMock(
            return_value=NS(messages=[NS(id=42, replies=NS(comments=True, replies=3))])
        ),
    )
    assert run(info(client, "channel", m)) == 3
    assert isinstance(client.invoke.call_args.args[0], raw.functions.channels.GetMessages)


def test_discussion_resolves_group_root_and_deduplicates(monkeypatch):
    from tgforward.comments import discussion

    root = NS(id=100, chat=NS(id=-1009))
    replies = [NS(id=9), NS(id=8)]
    c = NS(
        get_discussion_message=AsyncMock(return_value=root),
        resolve_peer=AsyncMock(return_value="group-peer"),
        invoke=AsyncMock(return_value=NS(messages=replies, users=[], chats=[])),
    )

    monkeypatch.setattr(
        discussion,
        "resolve_root",
        AsyncMock(
            return_value=discussion.ThreadRoot(
                root.chat.id, root.id, c.resolve_peer.return_value, frozenset({root.id})
            )
        ),
    )

    async def parse(_, m, users, chats, **kwargs):
        return NS(id=m.id, chat=root.chat, empty=False, media_group_id=None)

    monkeypatch.setattr(discussion.Message, "_parse", parse)
    messages, truncated = run(discussion.collect(c, "channel", 7))
    assert [m.id for m in messages] == [8, 9]
    request = c.invoke.call_args.args[0]
    assert request.peer == "group-peer" and request.msg_id == 100
    assert not truncated


def test_discussion_transfers_mixed_media_and_groups_once(monkeypatch):
    from tgforward.comments import discussion
    from tgforward.runtime import tasks
    from tgforward.storage.users import UserSettings

    replies = [
        NS(
            id=i,
            media_group_id="g" if i < 3 else None,
            chat=NS(id=-1009, username=None),
            media=kind,
        )
        for i, kind in enumerate(["photo", "video", "document", "audio"], 1)
    ]
    monkeypatch.setattr(discussion, "collect", AsyncMock(return_value=(replies, False)))
    from tests.transfers.test_routing import confirmed_message

    send = AsyncMock(side_effect=confirmed_message)
    monkeypatch.setattr(discussion.transfer, "transfer_message", send)
    summary = run(
        discussion.extract(
            None, None, "channel", 1, UserSettings(1), 1, tasks.Task(1, "comments", 1), None
        )
    )
    assert send.await_count == 3
    assert send.call_args_list[0].kwargs["media_group"] == replies[:2]
    assert "成功 4" in summary


def test_comment_partial_failure_is_visible(monkeypatch):
    from tgforward.comments import discussion
    from tgforward.runtime import tasks
    from tgforward.storage.users import UserSettings

    m = NS(id=1, media_group_id=None, chat=NS(id=-1009, username=None))
    monkeypatch.setattr(discussion, "collect", AsyncMock(return_value=([m], True)))
    monkeypatch.setattr(
        discussion.transfer, "transfer_message", AsyncMock(side_effect=ValueError("x"))
    )
    summary = run(
        discussion.extract(
            None, None, "c", 1, UserSettings(1), 1, tasks.Task(1, "comments", 1), None
        )
    )
    assert "失败 0" in summary and "最后错误：x" in summary and "未全部提取" in summary


def test_whitelist_paginates_without_credentials(monkeypatch):
    from tgforward.handlers import admin

    page = AsyncMock(return_value=[22])
    monkeypatch.setattr(admin, "count_whitelisted", AsyncMock(return_value=21))
    monkeypatch.setattr(admin, "list_whitelisted_page", page)
    text, markup = run(admin.whitelist_page(0))
    assert "21 人" in text and "`22`" in text and "1/2" in text
    page.assert_awaited_once_with(0, 20, exclude_owners=True)
    assert any(b.callback_data == "nav:list:1" for row in markup.inline_keyboard for b in row)


def test_auto_comments_runs_after_raw_detection(monkeypatch):
    from tgforward.runtime import tasks
    from tgforward.storage.users import UserSettings
    from tgforward.transfers import extractor
    from tgforward.utils.links import MessageLink

    session, uploader = NS(), NS()
    status = message(2)
    request = message(1)
    request.reply.return_value = status
    source = NS(id=7, chat=NS(type=enums.ChatType.CHANNEL), _client=session)
    settings = UserSettings(1, auto_comments=True)
    monkeypatch.setattr(extractor.discussion, "post_info", AsyncMock(return_value=(7, 2)))
    monkeypatch.setattr(extractor.clients, "get_user_client", AsyncMock(return_value=session))
    monkeypatch.setattr(extractor.clients, "bot", uploader)
    extracted = AsyncMock(return_value="评论提取：成功 2 条，失败 0 条。")
    monkeypatch.setattr(extractor.discussion, "extract", extracted)

    async def check():
        ref = MessageLink("c", 7, False)
        metadata = await extractor._comment_metadata(ref, source, settings)
        await extractor._extract_comments(
            request, ref, settings, uploader, tasks.Task(1, "single", 1), status, metadata
        )
        extracted.assert_awaited_once()

    run(check())


def test_helper_does_not_send_companion_comment_message(monkeypatch):
    from tgforward.runtime import tasks
    from tgforward.storage.users import UserSettings
    from tgforward.transfers import extractor
    from tgforward.utils.links import MessageLink

    main_bot = NS(send_message=AsyncMock())
    monkeypatch.setattr(extractor.clients, "bot", main_bot)
    run(
        extractor._extract_comments(
            message(),
            MessageLink("c", 7, False),
            UserSettings(1),
            NS(),
            tasks.Task(1, "single", 1),
            None,
            "kb",
        )
    )
    main_bot.send_message.assert_not_awaited()


def test_comment_pagination_stops_repeating_page(monkeypatch):
    from tgforward.comments import discussion

    root = NS(id=1000, chat=NS(id=-1009))
    raw_messages = [NS(id=i) for i in range(1, 101)]
    c = NS(
        get_discussion_message=AsyncMock(return_value=root),
        resolve_peer=AsyncMock(return_value="peer"),
        invoke=AsyncMock(return_value=NS(messages=raw_messages, users=[], chats=[])),
    )

    monkeypatch.setattr(
        discussion,
        "resolve_root",
        AsyncMock(
            return_value=discussion.ThreadRoot(
                root.chat.id, root.id, c.resolve_peer.return_value, frozenset({root.id})
            )
        ),
    )

    async def parse(_, m, *a, **kw):
        return NS(id=m.id, chat=root.chat)

    monkeypatch.setattr(discussion.Message, "_parse", parse)
    with pytest.raises(discussion.transfer.TransferError, match="分页没有前进"):
        run(discussion.collect(c, "c", 1))
    assert c.invoke.await_count == 2


def test_comment_limit_is_not_silent(monkeypatch):
    from tgforward.comments import discussion

    monkeypatch.setattr(discussion, "MAX_REPLIES", 2)
    root = NS(id=1000, chat=NS(id=-1009))
    c = NS(
        get_discussion_message=AsyncMock(return_value=root),
        resolve_peer=AsyncMock(return_value="peer"),
        invoke=AsyncMock(
            return_value=NS(messages=[NS(id=i) for i in [4, 3, 2]], users=[], chats=[])
        ),
    )

    monkeypatch.setattr(
        discussion,
        "resolve_root",
        AsyncMock(
            return_value=discussion.ThreadRoot(
                root.chat.id, root.id, c.resolve_peer.return_value, frozenset({root.id})
            )
        ),
    )

    async def parse(_, m, *a, **kw):
        return NS(id=m.id, chat=root.chat, media_group_id=None)

    monkeypatch.setattr(discussion.Message, "_parse", parse)
    messages, truncated = run(discussion.collect(c, "c", 1))
    assert len(messages) == 2 and truncated


@pytest.mark.parametrize("album", [False, True])
def test_empty_copy_result_does_not_report_success(monkeypatch, album):
    from tests.transfers.test_routing import FakeDownloader, FakeUploader, _media_message
    from tgforward.storage.users import UserSettings
    from tgforward.transfers import transfer

    m = _media_message(photo=NS(file_size=1), media_group_id="g" if album else None)
    up = FakeUploader()
    up.copy_message = AsyncMock(return_value=None)
    up.copy_media_group = AsyncMock(return_value=[])
    from tests.transfers.test_routing import confirmed_physical

    physical = AsyncMock(side_effect=confirmed_physical)
    monkeypatch.setattr(transfer, "_transfer_physical", physical)
    monkeypatch.setattr(transfer, "_send_album_physical", physical)
    with pytest.raises(transfer.TransferError, match="公开"):
        run(
            transfer.transfer_message(
                up, FakeDownloader(group=[m]), m, UserSettings(1), "1", source_private=False
            )
        )
    physical.assert_not_awaited()


def test_external_target_does_not_duplicate_comment_entry(monkeypatch):
    from tgforward.runtime import tasks
    from tgforward.storage.users import UserSettings
    from tgforward.transfers import extractor
    from tgforward.utils.links import MessageLink

    main_bot = NS(send_message=AsyncMock())
    monkeypatch.setattr(extractor.clients, "bot", main_bot)
    run(
        extractor._extract_comments(
            message(),
            MessageLink("c", 7, False),
            UserSettings(1, chat_id="-100123"),
            main_bot,
            tasks.Task(1, "single", 1),
            None,
            "kb",
        )
    )
    main_bot.send_message.assert_not_awaited()
