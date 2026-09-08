"""用户输入、菜单切换与进度反馈。"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest
from pyrogram.errors import ChatForwardsRestricted

from tgforward.handlers import admin, auth, comments, relay, router, settings
from tgforward.runtime import tasks
from tgforward.storage.users import UserSettings
from tgforward.telegram.clients import bot
from tgforward.transfers import transfer
from tgforward.ui import state


def msg(text="", uid=1):
    status = NS(id=10, chat=NS(id=uid), edit=AsyncMock(), delete=AsyncMock())
    return NS(
        id=1,
        text=text,
        caption=None,
        from_user=NS(id=uid),
        chat=NS(id=uid),
        reply=AsyncMock(return_value=status),
        delete=AsyncMock(),
        edit=AsyncMock(),
        command=text.lstrip("/").split() if text.startswith("/") else None,
        media_group_id=None,
        photo=None,
    )


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(settings, "get_user", AsyncMock(return_value={}))
    state._states.clear()
    tasks._tasks.clear()
    tasks._last_finished.clear()
    yield
    state._states.clear()
    tasks._tasks.clear()
    tasks._last_finished.clear()


def registered_filter(callback):
    bot.dispatcher.loop.run_until_complete(asyncio.sleep(0))
    for group in bot.dispatcher.groups.values():
        for handler in group:
            if getattr(handler, "original_callback", None) is callback:
                return handler
    raise AssertionError("callback not registered")


@pytest.mark.parametrize(
    "callback,data",
    [(settings.settings_callback, "set:rename"), (comments.on_fetch_comments, "cmt:example:1")],
)
def test_real_callback_filter_signature(callback, data):
    handler = registered_filter(callback)
    query = NS(data=data, from_user=NS(id=1, username=None), message=None, inline_message_id=None)

    async def run():
        with patch.object(
            handler, "check_if_has_matching_listener", AsyncMock(return_value=(False, None))
        ):
            assert await handler.check(bot, query)
            query.data = "unrelated"
            assert not await handler.check(bot, query)

    bot.loop.run_until_complete(run())


def test_settings_button_starts_user_dialogue(monkeypatch):
    monkeypatch.setattr(settings, "is_whitelisted", AsyncMock(return_value=True))
    m = msg(uid=99)
    m.chat.id = 1
    q = NS(data="set:rename", from_user=NS(id=1), message=m, answer=AsyncMock())
    asyncio.run(settings.settings_callback(None, q))
    assert state.get(1).kind == "settings"
    assert state.get(99) is None
    assert m.edit.call_args.kwargs["reply_markup"] is not None


def test_login_repeated_bad_input_always_replies():
    m = msg("https://t.me/example/1")
    st = state.set(1, "login", "phone", status_msg=NS(edit=AsyncMock()))

    async def run():
        await auth.handle_login_steps(None, m)
        await auth.handle_login_steps(None, m)

    asyncio.run(run())
    assert m.reply.await_count == 2
    for call in m.reply.call_args_list:
        assert "退出" in call.args[0]
        assert call.kwargs["reply_markup"] is not None
    assert state.get(1) is st


def test_unbind_absent_is_not_success(monkeypatch):
    monkeypatch.setattr(relay, "ensure_whitelisted", AsyncMock(return_value=True))
    monkeypatch.setattr(relay, "get_helper_token", AsyncMock(return_value=None), raising=False)
    stop, remove = AsyncMock(), AsyncMock(return_value=True)
    monkeypatch.setattr(relay, "remove_helper_bot", stop)
    monkeypatch.setattr(relay, "remove_helper_token", remove)
    m = msg("/unbindbot")
    asyncio.run(relay.unbind_bot(None, m))
    assert "无需解绑" in m.reply.call_args.args[0]
    stop.assert_not_awaited()
    remove.assert_not_awaited()


def test_bind_without_arg_asks_for_token(monkeypatch):
    monkeypatch.setattr(relay, "ensure_whitelisted", AsyncMock(return_value=True))
    m = msg("/bindbot")
    asyncio.run(relay.bind_bot(None, m))
    assert state.get(1).kind == "helper"
    assert m.reply.call_args.kwargs["reply_markup"]


def test_bind_validates_before_saving(monkeypatch):
    monkeypatch.setattr(relay, "ensure_whitelisted", AsyncMock(return_value=True))
    candidate = NS(
        connect=AsyncMock(),
        sign_in_bot=AsyncMock(return_value=NS(is_bot=True, username="helper")),
        disconnect=AsyncMock(),
    )
    monkeypatch.setattr(relay, "_client", lambda *a, **kw: candidate, raising=False)
    save, stop = AsyncMock(return_value=True), AsyncMock()
    monkeypatch.setattr(relay, "save_helper_token", save)
    monkeypatch.setattr(relay, "remove_helper_bot", stop)
    m = msg("/bindbot 123456:" + "A" * 30)
    asyncio.run(relay.bind_bot(None, m))
    candidate.sign_in_bot.assert_awaited_once()
    candidate.disconnect.assert_awaited_once()
    save.assert_awaited_once()
    assert state.get(1) is None


def test_bad_token_does_not_replace_binding(monkeypatch):
    monkeypatch.setattr(relay, "ensure_whitelisted", AsyncMock(return_value=True))
    candidate = NS(
        connect=AsyncMock(),
        sign_in_bot=AsyncMock(side_effect=ValueError("invalid")),
        disconnect=AsyncMock(),
    )
    monkeypatch.setattr(relay, "_client", lambda *a, **kw: candidate, raising=False)
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(relay, "save_helper_token", save)
    monkeypatch.setattr(relay, "remove_helper_bot", AsyncMock())
    m = msg("/bindbot 123456:" + "A" * 30)
    asyncio.run(relay.bind_bot(None, m))
    save.assert_not_awaited()
    assert state.get(1).kind == "helper"


def test_allow_is_dialogue_and_retains_argument_form(monkeypatch):
    monkeypatch.setattr(admin, "OWNER_ID", [1])
    saved = AsyncMock(return_value=True)
    monkeypatch.setattr(admin, "set_whitelisted", saved)
    client = NS(send_message=AsyncMock())

    async def run():
        m = msg("/allow")
        await admin.allow_user(client, m)
        assert state.get(1).kind == "admin"
        await admin.admin_user_input(client, msg("invalid"))
        assert state.get(1) is not None
        await admin.admin_user_input(client, msg("42"))
        assert state.get(1) is None
        await admin.allow_user(client, msg("/allow 43"))

    asyncio.run(run())
    assert [c.args for c in saved.call_args_list] == [(42, True), (43, True)]


def test_cancel_button_is_owner_and_flow_scoped():
    from tgforward.handlers.cancel import cancel_callback
    from tgforward.ui.dialogue import cancel_keyboard

    st = state.set(1, "login", "phone")
    data = cancel_keyboard(1).inline_keyboard[0][0].callback_data
    q = NS(data=data, from_user=NS(id=2), message=msg(), answer=AsyncMock())

    async def run():
        await cancel_callback(None, q)
        assert state.get(1) is st
        newer = state.set(1, "helper", "token")
        q.from_user.id = 1
        await cancel_callback(None, q)
        assert state.get(1) is newer
        q.data = cancel_keyboard(1).inline_keyboard[0][0].callback_data
        await cancel_callback(None, q)
        assert state.get(1) is None

    asyncio.run(run())


def media(album=False):
    return NS(
        id=10,
        chat=NS(id=-100123, username=None),
        text=None,
        media=True,
        media_group_id="g" if album else None,
        caption=None,
        photo=NS(file_size=100),
        video=None,
        audio=None,
        document=None,
        animation=None,
        sticker=None,
        voice=None,
        video_note=None,
    )


def test_copy_only_never_downloads(monkeypatch):
    s = UserSettings(1)
    up = NS(copy_message=AsyncMock(side_effect=ChatForwardsRestricted()))
    physical = AsyncMock()
    monkeypatch.setattr(transfer, "_transfer_physical", physical)

    async def run():
        with pytest.raises(transfer.TransferError, match="公开来源不下载"):
            await transfer.transfer_message(up, None, media(), s, "1", source_private=False)

    asyncio.run(run())
    physical.assert_not_awaited()


def test_router_returns_without_waiting_for_transfer(monkeypatch):
    monkeypatch.setattr(router, "is_whitelisted", AsyncMock(return_value=True))

    async def run():
        started = asyncio.Event()

        async def slow(*a, **kw):
            started.set()
            await kw["task"].wait_or_cancel(3600)

        monkeypatch.setattr(router, "extract_single", slow)
        first = msg("https://t.me/example/1")
        await asyncio.wait_for(router.smart_router(None, first), 0.2)
        await started.wait()
        task = tasks.get(1)
        second = msg("https://t.me/example/2")
        await router.smart_router(None, second)
        assert "当前请求未加入" in second.reply.call_args.args[0]
        tasks.request_cancel(1)
        await task.runner
        assert not tasks.is_active(1)

    asyncio.run(run())


def test_stalled_background_task_is_released(monkeypatch):
    monkeypatch.setattr(tasks, "TASK_STALL_TIMEOUT", 0.03, raising=False)

    async def run():
        task = tasks.register(1, "single", 1)
        reply = AsyncMock()
        tasks.launch(task, lambda: asyncio.Event().wait(), reply)
        await asyncio.wait_for(task.runner, 0.5)
        assert not tasks.is_active(1)
        assert any("长时间没有进度" in c.args[0] for c in reply.call_args_list)

    asyncio.run(run())


def test_unexpected_copy_error_is_not_download(monkeypatch):
    up = NS(copy_message=AsyncMock(side_effect=TypeError("programming error")))
    physical = AsyncMock()
    monkeypatch.setattr(transfer, "_transfer_physical", physical)

    async def run():
        with pytest.raises(transfer.TransferError, match="公开来源不下载"):
            await transfer.transfer_message(
                up, None, media(), UserSettings(1), "1", source_private=False
            )

    asyncio.run(run())
    physical.assert_not_awaited()


def test_oversize_album_can_copy_without_premium(monkeypatch):
    m = media(True)
    m.photo.file_size = transfer.LARGE_FILE_BYTES + 1
    up = NS(copy_media_group=AsyncMock(return_value=[NS(id=1)]))
    dl = NS(get_messages=AsyncMock(return_value=[m]))
    monkeypatch.setattr(transfer.clients_registry, "premium", None)
    asyncio.run(transfer.transfer_message(up, dl, m, UserSettings(1), "1", source_private=False))
    up.copy_media_group.assert_awaited_once()


@pytest.mark.parametrize("mode", ["auto", "copy", "download"])
def test_transfer_setting_persists_mode(monkeypatch, mode):
    monkeypatch.setattr(settings, "is_whitelisted", AsyncMock(return_value=True))
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(settings, "set_field", save)
    q = NS(data=f"set:mode:{mode}", from_user=NS(id=1), message=msg(), answer=AsyncMock())
    asyncio.run(settings.settings_callback(None, q))
    save.assert_not_awaited()


def test_caption_changes_do_not_force_photo_download(monkeypatch):
    up = NS(copy_message=AsyncMock())
    m = media()
    m.caption = "hello"
    s = UserSettings(1, caption="footer")
    asyncio.run(transfer.transfer_message(up, None, m, s, "1", source_private=False))
    assert up.copy_message.call_args.kwargs["caption"] == "hello\n\nfooter"


def test_task_cancel_before_first_run_releases_registration():
    async def run():
        task = tasks.register(1, "single", 1)
        work = AsyncMock()
        tasks.launch(task, work, AsyncMock())
        tasks.request_cancel(1)
        await task.runner
        assert task.cancel_reason == tasks.CancelReason.USER
        await asyncio.sleep(0)
        assert not tasks.is_active(1)
        work.assert_not_awaited()
        tasks.register(1, "single", 1)  # 取消后无需等冷却期

    asyncio.run(run())


def test_progress_uses_status_owner_and_updates_activity():
    from tgforward.transfers.progress import make_progress

    async def run():
        task = tasks.register(1, "single", 1)
        task.last_activity = 0
        up = NS(edit_message_text=AsyncMock())
        status = NS(edit=AsyncMock())
        cb = make_progress(up, 1, 10, task, status_message=status)
        await cb(1, 10)
        assert task.last_activity > 0
        status.edit.assert_awaited_once()
        up.edit_message_text.assert_not_awaited()

    asyncio.run(run())


def test_private_source_accessible_to_bot_does_not_require_login():
    from tgforward.transfers.extractor import fetch_message
    from tgforward.utils.links import MessageLink

    fetched = NS(empty=False)
    up = NS(get_messages=AsyncMock(return_value=fetched))

    async def run():
        assert await fetch_message(up, None, MessageLink("-100123", 1, True), user_id=1) is fetched

    asyncio.run(run())


def test_new_command_is_not_consumed_by_any_dialogue():
    filters = [
        registered_filter(callback).filters
        for callback in (
            auth.handle_login_steps,
            relay.bind_bot_input,
            settings.handle_settings_input,
            admin.admin_user_input,
        )
    ]
    from pyrogram import enums, types

    async def run():
        for kind in ("login", "helper", "settings", "admin"):
            state.set(1, kind, "input")
            m = types.Message(
                id=1,
                text="/start",
                from_user=types.User(id=1),
                chat=types.Chat(id=1, type=enums.ChatType.PRIVATE),
            )
            for f in filters:
                assert not await f(bot, m)

    bot.loop.run_until_complete(run())


def test_thumb_setting_uses_supported_message_download(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    downloaded = tmp_path / "photo.jpg"
    from PIL import Image

    from tgforward.utils import media

    monkeypatch.setattr(media, "DATA_DIR", str(tmp_path / "data"))
    Image.new("RGB", (640, 480), "red").save(downloaded)
    m = msg()
    m.photo = True
    m.download = AsyncMock(return_value=str(downloaded))
    state.set(1, "settings", "thumb")
    asyncio.run(settings.handle_settings_input(None, m))
    with Image.open(media.thumbnail_path(1)) as thumb:
        assert thumb.format == "JPEG" and thumb.size == (320, 240)
    assert not downloaded.exists()
    assert state.get(1) is None


@pytest.mark.parametrize(
    "action", ["rename", "thumb", "caption", "replacement", "deleteword", "chat"]
)
def test_all_settings_input_buttons_start_dialogue(monkeypatch, action):
    monkeypatch.setattr(settings, "is_whitelisted", AsyncMock(return_value=True))
    q = NS(data=f"set:{action}", from_user=NS(id=1), message=msg(), answer=AsyncMock())
    asyncio.run(settings.settings_callback(None, q))
    assert state.get(1).step == action
    q.answer.assert_awaited_once()
    assert q.message.edit.call_args.kwargs["reply_markup"]


@pytest.mark.parametrize("action", ["comments", "reset", "remthumb", "mode"])
def test_settings_immediate_buttons_respond(monkeypatch, action):
    monkeypatch.setattr(settings, "get_user", AsyncMock(return_value={}))
    monkeypatch.setattr(settings, "is_whitelisted", AsyncMock(return_value=True))
    monkeypatch.setattr(settings, "toggle_auto_comments", AsyncMock(return_value=True))
    monkeypatch.setattr(settings, "set_field", AsyncMock(return_value=True))
    monkeypatch.setattr(settings, "reset_settings", AsyncMock(return_value=True))
    monkeypatch.setattr(settings, "remove_custom_thumb", lambda _: "removed")
    q = NS(data=f"set:{action}", from_user=NS(id=1), message=msg(), answer=AsyncMock())
    asyncio.run(settings.settings_callback(None, q))
    q.answer.assert_awaited_once()
    q.message.edit.assert_awaited_once()
    q.message.reply.assert_not_awaited()


def test_progressing_long_task_does_not_time_out(monkeypatch):
    monkeypatch.setattr(tasks, "TASK_STALL_TIMEOUT", 0.05)

    async def run():
        task = tasks.register(1, "single", 1)

        async def work():
            for _ in range(20):
                task.touch("下载")
                await asyncio.sleep(0.005)

        tasks.launch(task, work, AsyncMock())
        await asyncio.wait_for(task.runner, 1)
        assert not task.timed_out
        assert not tasks.is_active(1)

    asyncio.run(run())


def test_album_download_honors_name_rules_and_upload_progress(monkeypatch, tmp_path):
    first = tmp_path / "one.bin"
    first.write_bytes(b"1")
    second = tmp_path / "two.bin"
    second.write_bytes(b"2")
    group = [media(True), media(True)]
    for index, m in enumerate(group):
        m.photo = None
        m.document = NS(file_name=f"{index}.bin", file_size=1)
        m.id += index
    dl = NS(download_media=AsyncMock(side_effect=[str(first), str(second)]))
    up = NS(send_media_group=AsyncMock())
    status = NS(id=1, chat=NS(id=1), edit=AsyncMock())
    s = UserSettings(1, rename_tag="tag")
    asyncio.run(
        transfer._send_album_physical(
            up, dl, group, [None, None], ["", ""], s, "1", 1, None, None, status
        )
    )
    sent = up.send_media_group.call_args
    assert [item.media for item in sent.args[1]] == [
        str(tmp_path / "one tag.bin"),
        str(tmp_path / "two tag.bin"),
    ]
    assert callable(sent.kwargs["progress"])
    assert not list(tmp_path.iterdir())


def test_binding_cancelled_during_verification_does_not_save(monkeypatch):
    from tgforward.ui import dialogue

    monkeypatch.setattr(relay, "ensure_whitelisted", AsyncMock(return_value=True))
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(relay, "save_helper_token", save)

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def verify(_):
            entered.set()
            await release.wait()
            return NS(is_bot=True, username="helper")

        candidate = NS(connect=AsyncMock(), sign_in_bot=verify, disconnect=AsyncMock())
        monkeypatch.setattr(relay, "_client", lambda *a, **kw: candidate)
        pending = asyncio.create_task(relay.bind_bot(None, msg("/bindbot 123456:" + "A" * 30)))
        await entered.wait()
        await dialogue.clear(1)
        newer = state.set(1, "settings", "caption")
        release.set()
        await pending
        save.assert_not_awaited()
        assert state.get(1) is newer

    asyncio.run(run())
