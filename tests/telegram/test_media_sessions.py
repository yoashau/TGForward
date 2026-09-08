"""验证限流重试、实际 get_file 会话复用，以及非最小 ID 评论入口。"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pyrogram import Client, enums, raw
from pyrogram.errors import FloodWait, MessageIdInvalid
from pyrogram.file_id import FileType

from tgforward.comments import discussion
from tgforward.runtime.tasks import Task
from tgforward.telegram import media_session
from tgforward.transfers import downloads


def fake_client():
    return NS(
        name="test-user",
        media_sessions={},
        media_sessions_lock=asyncio.Lock(),
        get_file_semaphore=asyncio.Semaphore(2),
        storage=NS(
            dc_id=AsyncMock(return_value=2),
            test_mode=AsyncMock(return_value=False),
            auth_key=AsyncMock(return_value=b"key"),
        ),
        invoke=AsyncMock(return_value=NS(id=1, bytes=b"auth")),
    )


def fake_sessions(monkeypatch):
    instances = []

    def session(*args, **kwargs):
        s = NS(
            start=AsyncMock(),
            stop=AsyncMock(),
            invoke=AsyncMock(
                return_value=raw.types.upload.File(
                    type=raw.types.storage.FileUnknown(), mtime=0, bytes=b"file"
                )
            ),
        )
        instances.append(s)
        return s

    monkeypatch.setattr(media_session, "Session", session)
    monkeypatch.setattr(media_session, "Auth", lambda *a: NS(create=AsyncMock(return_value=b"key")))
    return instances


def file_id():
    return NS(
        file_type=FileType.DOCUMENT,
        dc_id=4,
        media_id=1,
        access_hash=2,
        file_reference=b"ref",
        thumbnail_size="",
    )


def test_34_actual_get_file_downloads_export_once_and_close_at_terminate(monkeypatch):
    instances = fake_sessions(monkeypatch)

    async def check():
        c = fake_client()
        for _ in range(34):
            assert b"".join([chunk async for chunk in Client.get_file(c, file_id())]) == b"file"
        assert c.invoke.await_count == 1
        assert isinstance(c.invoke.call_args.args[0], raw.functions.auth.ExportAuthorization)
        assert len(instances) == 1
        instances[0].stop.assert_not_awaited()
        assert (
            sum(
                isinstance(call.args[0], raw.functions.auth.ImportAuthorization)
                for call in instances[0].invoke.call_args_list
            )
            == 1
        )
        # Execute the library lifecycle method, not a simulated cache clear.
        c.is_initialized, c.takeout_id = True, None
        c.storage.save = AsyncMock()
        c.dispatcher = NS(stop=AsyncMock())
        c.updates_watchdog_event = asyncio.Event()
        c.updates_watchdog_task = None
        from pyrogram.methods.auth.terminate import Terminate

        await Terminate.terminate(c)
        instances[0].stop.assert_awaited_once()
        assert not c.media_sessions

    asyncio.run(check())


def test_concurrent_session_creation_and_account_isolation(monkeypatch):
    instances = fake_sessions(monkeypatch)

    async def check():
        a, b = fake_client(), fake_client()
        sessions = await asyncio.gather(*(media_session.media_session(a, 4) for _ in range(8)))
        assert all(s is sessions[0] for s in sessions)
        assert await media_session.media_session(b, 4) is not sessions[0]
        assert len(instances) == 2
        assert a.invoke.await_count == b.invoke.await_count == 1

    asyncio.run(check())


def test_failed_session_creation_is_not_cached(monkeypatch):
    instances = fake_sessions(monkeypatch)
    original = media_session.Session

    def session(*args, **kwargs):
        s = original(*args, **kwargs)
        s.start.side_effect = FloodWait(3)
        return s

    monkeypatch.setattr(media_session, "Session", session)

    async def check():
        c = fake_client()
        with pytest.raises(FloodWait):
            await media_session.media_session(c, 4)
        assert c.media_sessions == {}
        instances[0].stop.assert_awaited_once()

    asyncio.run(check())


def test_download_wait_retries_same_file_before_next_and_keeps_task_alive(monkeypatch):
    now, calls, waits = [0.0], [], []
    real_sleep = asyncio.sleep
    monkeypatch.setattr(downloads.time, "monotonic", lambda: now[0])

    async def sleep(seconds):
        now[0] += seconds
        waits.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(downloads.asyncio, "sleep", sleep)
    task = Task(1, "comments", 34)
    status = NS(edit=AsyncMock())

    async def download(message, **kwargs):
        calls.append(message.id)
        if len(calls) == 1:
            raise FloodWait(600)
        return "file"

    async def check():
        c = NS(name="user", download_media=download)
        await asyncio.gather(
            downloads.download_media(c, NS(id=1), task=task, status=status),
            downloads.download_media(c, NS(id=2), task=Task(2, "single", 1)),
        )
        assert calls == [1, 1, 2]
        assert sum(waits) >= 601
        assert not task.media_failed
        assert task.last_activity >= 600 and not task.timed_out
        assert "自动继续当前媒体" in status.edit.call_args.args[0]

    asyncio.run(check())


def test_cancel_during_wait_releases_lock_but_preserves_account_cooldown(monkeypatch):
    entered = asyncio.Event()

    async def sleep(_):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(downloads.asyncio, "sleep", sleep)

    async def check():
        c = NS(name="user", download_media=AsyncMock(side_effect=FloodWait(600)))
        task = Task(1, "single", 1)
        runner = asyncio.create_task(downloads.download_media(c, NS(id=1), task=task))
        await entered.wait()
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        queue = downloads.queue_for(c)
        assert not queue.lock.locked()
        assert queue.blocked_until > downloads.time.monotonic()
        c.download_media.assert_awaited_once()

    asyncio.run(check())


@pytest.mark.parametrize("fallback", [False, True])
def test_resolver_confirms_non_minimum_entry_and_falls_back_by_rpc(fallback, caplog):
    attempts = []
    gid = 2659469493

    async def invoke(req):
        if isinstance(req, raw.functions.channels.GetMessages):
            assert [m.id for m in req.id] == [9682, 9683, 9691]
            return NS(
                messages=[
                    NS(id=9682, replies=None),
                    NS(id=9691, replies=None),
                    NS(id=9683, replies=NS(comments=True, channel_id=gid, replies=34)),
                ]
            )
        assert isinstance(req, raw.functions.messages.GetDiscussionMessage)
        attempts.append(req.msg_id)
        if req.msg_id != (9691 if fallback else 9683):
            raise MessageIdInvalid()
        return NS(
            chats=[NS(id=gid, megagroup=True, access_hash=7), NS(id=9, megagroup=True)],
            messages=[
                NS(id=80244, peer_id=raw.types.PeerChannel(channel_id=gid), grouped_id="g"),
                NS(id=80243, peer_id=raw.types.PeerChannel(channel_id=gid), grouped_id="g"),
                NS(id=1, peer_id=raw.types.PeerChannel(channel_id=9)),
            ],
        )

    c = NS(
        get_messages=AsyncMock(
            return_value=NS(id=9691, media_group_id="g", chat=NS(type=enums.ChatType.CHANNEL))
        ),
        get_media_group=AsyncMock(return_value=[NS(id=i) for i in [9682, 9683, 9691]]),
        resolve_peer=AsyncMock(
            return_value=raw.types.InputPeerChannel(channel_id=5, access_hash=6)
        ),
        invoke=AsyncMock(side_effect=invoke),
    )
    with caplog.at_level("INFO"):
        root = asyncio.run(discussion.resolve_root(c, "Cos_zg", 9691))
    assert attempts == ([9683, 9691] if fallback else [9683])
    assert root.chat_id == -1002659469493 and root.message_id == 80243
    assert "input=9691 album=[9682, 9683, 9691]" in caplog.text
    assert f"comment_post={attempts[-1]} discussion_chat=-1002659469493 root=80243" in caplog.text


@pytest.mark.parametrize("album", [False, True])
def test_single_and_album_physical_paths_use_same_retry(monkeypatch, tmp_path, album):
    from tests.transfers.test_routing import FakeUploader, _media_message
    from tgforward.storage.users import UserSettings
    from tgforward.transfers import transfer

    calls = []
    messages = [
        _media_message(id=i, photo=NS(file_size=1), media_group_id="g" if album else None)
        for i in range(1, 3 if album else 2)
    ]

    async def download(message, **kwargs):
        calls.append(message.id)
        if len(calls) == 1:
            raise FloodWait(0)
        path = tmp_path / f"{message.id}.jpg"
        path.write_bytes(b"x")
        return str(path)

    async def wait(queue, *args):
        queue.blocked_until = 0  # waiting duration/cancellation tested separately

    monkeypatch.setattr(downloads, "_wait", wait)
    monkeypatch.setattr(transfer, "_MIN_SEND_INTERVAL", 0)
    c = NS(name="user", download_media=download)
    task = Task(1, "single", 1)
    status = NS(id=1, chat=NS(id=1), edit=AsyncMock())
    asyncio.run(
        transfer.transfer_message(
            FakeUploader(),
            c,
            messages[0],
            UserSettings(1),
            "1",
            source_private=True,
            task=task,
            status=status,
            media_group=messages if album else None,
        )
    )
    assert calls == ([1, 1, 2] if album else [1, 1])
    assert not task.media_failed
    assert len(task.media_downloaded) == len(task.media_sent) == len(messages)


def test_partial_download_cleans_temp_and_reuses_session_on_retry(monkeypatch, tmp_path):
    from functools import partial

    instances = fake_sessions(monkeypatch)
    original_session = media_session.Session
    failed = [False]
    offsets = []
    chunk = b"x" * (1024 * 1024)

    async def invoke(request, **kwargs):
        if isinstance(request, raw.functions.auth.ImportAuthorization):
            return None
        offsets.append(request.offset)
        if request.offset and not failed[0]:
            failed[0] = True
            raise FloodWait(0)
        return raw.types.upload.File(
            type=raw.types.storage.FileUnknown(),
            mtime=0,
            bytes=chunk if request.offset == 0 else b"end",
        )

    def session(*args, **kwargs):
        s = original_session(*args, **kwargs)
        s.invoke = AsyncMock(side_effect=invoke)
        return s

    async def wait(queue, *args):
        queue.blocked_until = 0

    monkeypatch.setattr(media_session, "Session", session)
    monkeypatch.setattr(downloads, "_wait", wait)

    async def check():
        c = fake_client()
        c.get_file = partial(Client.get_file, c)

        async def download(message, **kwargs):
            return await Client.handle_download(
                c, (file_id(), str(tmp_path), "media.bin", False, len(chunk) + 3, None, ())
            )

        c.download_media = download
        path = await downloads.download_media(c, NS(id=1))
        from pathlib import Path

        assert Path(path).read_bytes() == chunk + b"end"
        assert not list(tmp_path.glob("*.temp"))
        assert offsets == [0, len(chunk), 0, len(chunk)]
        assert c.invoke.await_count == 1 and len(instances) == 1

    asyncio.run(check())
