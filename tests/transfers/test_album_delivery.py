"""相册部分成功、续传顺序与取消行为。"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pyrogram import enums, raw, utils
from pyrogram.errors import EntityBoundsInvalid, FloodWait

from tests.transfers.test_routing import FakeDownloader, FakeUploader, _media_message, _text_message
from tgforward.runtime import tasks
from tgforward.storage.users import UserSettings
from tgforward.telegram import clients
from tgforward.transfers import extractor, progress, transfer
from tgforward.utils.links import MessageLink


def status():
    return NS(id=91, chat=NS(id=1), edit=AsyncMock(), delete=AsyncMock())


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(transfer, "_send_lock", asyncio.Lock())
    monkeypatch.setattr(transfer, "_TRANSFER_SEMAPHORE", asyncio.Semaphore(4))
    monkeypatch.setattr(transfer, "_MIN_SEND_INTERVAL", 0)
    monkeypatch.setattr(transfer, "_last_send_at", 0)
    monkeypatch.setattr(tasks, "_tasks", {})
    monkeypatch.setattr(tasks, "_last_finished", {})


def test_all_long_text_chunks_stay_in_topic():
    send = AsyncMock(return_value=status())
    asyncio.run(
        transfer.transfer_message(
            NS(send_message=send),
            NS(),
            _text_message("x" * 9000),
            UserSettings(1, chat_id="-100123/9"),
            "1",
            source_private=False,
        )
    )
    assert send.await_count == 3
    assert all(c.kwargs.get("message_thread_id") == 9 for c in send.call_args_list)


def test_format_fallback_preserves_all_parameters():
    send = AsyncMock(side_effect=[EntityBoundsInvalid(), status()])
    asyncio.run(
        transfer.transfer_message(
            NS(send_message=send),
            NS(),
            _text_message("**text**"),
            UserSettings(1, chat_id="-100123/9"),
            "1",
            source_private=False,
        )
    )
    first, second = send.call_args_list
    assert second.args == first.args
    assert second.kwargs == {**first.kwargs, "parse_mode": enums.ParseMode.DISABLED}


def test_network_error_is_not_a_format_retry():
    send = AsyncMock(side_effect=TimeoutError("possibly accepted"))
    with pytest.raises(TimeoutError):
        asyncio.run(
            transfer.transfer_message(
                NS(send_message=send),
                NS(),
                _text_message(),
                UserSettings(1),
                "1",
                source_private=False,
            )
        )
    assert send.await_count == 1


@pytest.mark.parametrize("large", [False, True])
def test_voice_upload_preserves_caption(monkeypatch, large):
    sender = NS(send_voice=AsyncMock(return_value=status()))
    uploader = NS(copy_message=AsyncMock(return_value=status())) if large else sender
    if large:
        monkeypatch.setattr(clients, "premium", sender)
        monkeypatch.setattr(clients, "premium_started", True)
        monkeypatch.setattr(transfer, "LOG_GROUP", -100999)
    method = transfer._upload_large if large else transfer._upload_regular
    m = _media_message(voice=NS(file_size=1))
    asyncio.run(
        method(
            uploader,
            m,
            "voice.ogg",
            "processed caption",
            None,
            None,
            -100123,
            None,
            status(),
            tasks.Task(1, "single", 1),
        )
    )
    assert sender.send_voice.call_args.kwargs.get("caption") == "processed caption"


def mixed_group():
    return [
        _media_message(id=i, media_group_id="g", document=NS(file_size=size))
        for i, size in enumerate([1, transfer.LARGE_FILE_BYTES + 1, 1], 1)
    ]


@pytest.mark.parametrize("fail", [False, True])
def test_mixed_album_order_and_partial_member_state(monkeypatch, fail):
    group, delivered = mixed_group(), []

    async def single(*args, **kwargs):
        m = args[2]
        if fail and m.id == 2:
            raise transfer.TransferError("second segment failed")
        delivered.append(m.id)
        from tests.transfers.test_routing import confirmed_physical

        return await confirmed_physical(*args, **kwargs)

    async def album(*args, **kwargs):
        delivered.extend(m.id for m in args[2])

    monkeypatch.setattr(transfer, "_transfer_physical", single)
    monkeypatch.setattr(transfer, "_send_album_physical", album)
    monkeypatch.setattr(clients, "premium", NS())
    monkeypatch.setattr(clients, "premium_started", True)
    monkeypatch.setattr(transfer, "LOG_GROUP", -100999)
    task = tasks.Task(1, "single", 1)

    async def run():
        await transfer.transfer_message(
            NS(),
            NS(),
            group[0],
            UserSettings(1),
            "1",
            source_private=True,
            task=task,
            status=status(),
            media_group=group,
        )

    if fail:
        with pytest.raises(transfer.TransferError):
            asyncio.run(run())
        assert delivered == [1]
        assert task.media_sent == {(-100777, 1)} and not task.media_failed
        result = task.units[0].message_results[0]
        assert result.delivery.not_attempted == {"media:2", "media:3"}
    else:
        asyncio.run(run())
        assert delivered == [1, 2, 3]


def test_batch_failed_group_does_not_turn_later_members_into_success(monkeypatch):
    group = [_media_message(id=i, media_group_id="g", photo=NS(file_size=1)) for i in (1, 2, 3)]

    async def fetch(*args, message_id=None, **kwargs):
        return group[message_id - 1]

    monkeypatch.setattr(extractor, "fetch_message", fetch)
    monkeypatch.setattr(transfer, "_fetch_media_group", AsyncMock(return_value=group))
    monkeypatch.setattr(clients, "get_user_client", AsyncMock(return_value=NS()))
    monkeypatch.setattr(clients, "get_upload_bot", AsyncMock(return_value=NS()))
    monkeypatch.setattr(extractor, "load_user_settings", AsyncMock(return_value=UserSettings(1)))
    monkeypatch.setattr(extractor, "_comment_metadata", AsyncMock(return_value=None))
    monkeypatch.setattr(
        transfer, "transfer_message", AsyncMock(side_effect=transfer.TransferError("failed"))
    )
    monkeypatch.setattr(extractor, "BATCH_DELAY", 0)
    result = status()
    request = NS(from_user=NS(id=1), chat=NS(id=1), reply=AsyncMock(return_value=result))
    task = tasks.Task(1, "batch", 3)
    asyncio.run(extractor.extract_range(request, MessageLink("-100777", 1, True), 3, task))
    assert task.success == 0
    assert "失败 3 条" in result.edit.call_args.args[0]


def test_single_member_never_calls_send_media_group(tmp_path):
    class Uploader(FakeUploader):
        async def send_media_group(self, chat_id, media, **kwargs):
            assert 2 <= len(media) <= 10, "real Telegram album contract"
            return await super().send_media_group(chat_id, media, **kwargs)

    path = tmp_path / "file.bin"
    path.write_bytes(b"x")
    m = _media_message(document=NS(file_size=1), media_group_id="g")
    uploader = Uploader()
    asyncio.run(
        transfer._send_album_physical(
            uploader,
            FakeDownloader(download_result=str(path)),
            [m],
            [None],
            ["caption"],
            UserSettings(1),
            "1",
            1,
            None,
            tasks.Task(1, "single", 1),
            status(),
        )
    )
    assert uploader.calls[0][0] == "send_document"


def test_long_floodwait_refreshes_heartbeat(monkeypatch):
    now, sleeps = [0.0], []
    real_sleep = asyncio.sleep
    monkeypatch.setattr(transfer.time, "monotonic", lambda: now[0])

    async def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        await real_sleep(0)

    monkeypatch.setattr(transfer.asyncio, "sleep", sleep)
    send = AsyncMock(side_effect=[FloodWait(600), status()])
    task = tasks.Task(1, "single", 1)
    asyncio.run(transfer._send(send, task))
    assert max(sleeps) <= 5 and task.last_activity >= 600


def test_existing_send_lock_is_already_released_before_rpc():
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        async def send():
            entered.set()
            await release.wait()
            return status()

        first = asyncio.create_task(transfer._send(send))
        await entered.wait()
        assert not transfer._send_lock.locked()
        await asyncio.wait_for(transfer._send(AsyncMock(return_value=status())), 0.3)
        release.set()
        await first

    asyncio.run(check())


def test_progress_leaves_no_global_throttle_entries():
    if hasattr(progress, "_throttle"):
        progress._throttle.clear()

    async def check():
        callback = progress.make_progress(
            NS(), 1, 91, tasks.Task(1, "single", 1), status_message=status()
        )
        await callback(10, 100)

    asyncio.run(check())
    assert not getattr(progress, "_throttle", {})


def test_real_copy_album_empty_caption_is_not_restored(monkeypatch):
    from pyrogram.methods.messages.copy_media_group import CopyMediaGroup

    async def parse(text):
        return {"message": str(text or ""), "entities": []}

    c = NS(
        parser=NS(parse=parse),
        rnd_id=lambda: 1,
        get_media_group=AsyncMock(
            return_value=[
                NS(photo=NS(file_id="photo"), caption="remove", has_media_spoiler=False),
                NS(photo=NS(file_id="photo"), caption="keep", has_media_spoiler=False),
            ]
        ),
        resolve_peer=AsyncMock(return_value=raw.types.InputPeerSelf()),
        invoke=AsyncMock(return_value=NS(updates=[], chats=[], users=[])),
    )
    monkeypatch.setattr(
        utils, "get_input_media_from_file_id", lambda **kwargs: raw.types.InputMediaEmpty()
    )
    monkeypatch.setattr(utils, "get_reply_to", AsyncMock(return_value=None))
    monkeypatch.setattr(
        utils, "parse_text_entities", AsyncMock(return_value={"message": "", "entities": []})
    )
    asyncio.run(CopyMediaGroup.copy_media_group(c, 1, 2, 3, captions=["", None]))
    assert [m.message for m in c.invoke.call_args.args[0].multi_media] == ["", "keep"]
