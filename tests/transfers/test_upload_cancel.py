import asyncio
from functools import partial
from importlib import import_module
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pyrogram.methods.advanced.save_file import SaveFile
from pyrogram.methods.messages.send_document import SendDocument
from pyrogram.methods.messages.send_media_group import SendMediaGroup

from tests.transfers.test_routing import _media_message
from tgforward.runtime import tasks
from tgforward.storage.users import UserSettings
from tgforward.transfers import transfer


@pytest.mark.parametrize("kind", ["single", "album", "premium"])
def test_sdk_upload_cancel_stops_delivery_and_cleans_downloads(monkeypatch, tmp_path, kind):
    """运行锁定版本的 save_file/send_*；只替换网络会话，覆盖两种中止返回方式。"""
    monkeypatch.setattr(transfer, "_send_lock", asyncio.Lock())
    monkeypatch.setattr(transfer, "_MIN_SEND_INTERVAL", 0)
    monkeypatch.setattr(transfer, "_last_send_at", 0)
    session = NS(start=AsyncMock(), invoke=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(
        import_module("pyrogram.methods.advanced.save_file"), "Session", lambda *a, **k: session
    )

    async def run():
        task = tasks.Task(901, "batch", 1)
        entered, release = asyncio.Event(), asyncio.Event()

        async def edit(*args, **kwargs):
            if task.uploading:
                entered.set()
                await release.wait()

        status = NS(id=2, chat=NS(id=901), edit=edit)
        sender = NS(
            loop=asyncio.get_running_loop(),
            save_file_semaphore=asyncio.Semaphore(1),
            me=NS(is_premium=kind == "premium"),
            rnd_id=lambda: 123,
            storage=NS(
                dc_id=AsyncMock(return_value=1), auth_key=AsyncMock(), test_mode=AsyncMock()
            ),
            invoke=AsyncMock(side_effect=AssertionError("cancelled upload must not send media")),
            copy_message=AsyncMock(),
        )
        sender.save_file = partial(SaveFile.save_file, sender)
        sender.send_document = partial(SendDocument.send_document, sender)
        sender.send_media_group = partial(SendMediaGroup.send_media_group, sender)
        count = 2 if kind == "album" else 1
        files, group = [], []
        for mid in range(1, count + 1):
            path = tmp_path / f"{mid}.bin"
            path.write_bytes(b"x" * (2 * 1024 * 1024))
            files.append(path)
            group.append(
                _media_message(
                    id=mid,
                    document=NS(file_size=path.stat().st_size),
                    media_group_id="group" if kind == "album" else None,
                )
            )
        if kind == "premium":
            monkeypatch.setattr(transfer, "LARGE_FILE_GIB", 0)
            monkeypatch.setattr(transfer, "LOG_GROUP", -100555)
            monkeypatch.setattr(transfer.clients_registry, "premium", sender)
            monkeypatch.setattr(transfer.clients_registry, "premium_started", True)
        downloader = NS(download_media=AsyncMock(side_effect=[str(path) for path in files]))
        uploading = asyncio.create_task(
            transfer.transfer_message(
                sender,
                downloader,
                group[0],
                UserSettings(901),
                "901",
                source_private=True,
                task=task,
                status=status,
                media_group=group if kind == "album" else None,
            )
        )
        await asyncio.wait_for(entered.wait(), 1)
        assert task.uploading and all(path.exists() for path in files)
        task.set_cancel_reason(tasks.CancelReason.USER)
        release.set()
        with pytest.raises(tasks.TaskCancelled):
            await asyncio.wait_for(uploading, 1)
        assert not task.uploading and not task.media_sent
        assert all(not path.exists() for path in files)
        sender.invoke.assert_not_awaited()
        sender.copy_message.assert_not_awaited()
        session.stop.assert_awaited_once()
        delivery = task.units[0].message_results[0].delivery
        assert not delivery.confirmed_delivered and not delivery.uncertain
        assert (
            delivery.not_attempted == {"media:1"}
            if kind == "premium"
            else not delivery.not_attempted
        )

    asyncio.run(run())
