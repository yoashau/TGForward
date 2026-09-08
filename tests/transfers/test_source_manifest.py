"""相册源读取失败必须在任何最终目标副作用之前中止。"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from tests.transfers.test_routing import _media_message
from tgforward.runtime.tasks import Task, TaskCancelled
from tgforward.storage.users import UserSettings
from tgforward.transfers import extractor, transfer
from tgforward.utils.links import MessageLink


def member(mid=10, **kwargs):
    return _media_message(id=mid, media_group_id="album", photo=NS(file_size=1), **kwargs)


@pytest.mark.parametrize(
    "error",
    [OSError("connection lost"), TimeoutError("read timeout"), ValueError("malformed source")],
)
def test_read_failure_is_explicit_and_preserves_cause(error):
    downloader = NS(get_messages=AsyncMock(side_effect=error))
    with pytest.raises(transfer.SourceResolutionError, match="完整相册") as caught:
        asyncio.run(transfer._fetch_media_group(downloader, member()))
    assert caught.value.__cause__ is error


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        [None],
        [NS(empty=True)],
        [member(11)],
        [member(), member()],
        [member(chat=NS(id=-100999))],
        [member(30), member()],
    ],
)
def test_invalid_manifest_is_not_a_single_member_success(response):
    downloader = NS(get_messages=AsyncMock(return_value=response))
    with pytest.raises(transfer.SourceResolutionError):
        asyncio.run(transfer._fetch_media_group(downloader, member()))


def test_valid_manifest_keeps_complete_sorted_members_and_excludes_other_posts():
    other = member(13)
    other.media_group_id = "other"
    downloader = NS(
        get_messages=AsyncMock(return_value=[member(12), None, other, member(10), member(11)])
    )
    group = asyncio.run(transfer._fetch_media_group(downloader, member()))
    assert [item.id for item in group] == [10, 11, 12]


def test_actual_single_surviving_member_is_not_an_exception_fallback():
    source = member()
    downloader = NS(get_messages=AsyncMock(return_value=[source]))
    group = asyncio.run(transfer._fetch_media_group(downloader, source))
    assert group == [source]
    downloader.get_messages.assert_awaited_once()


@pytest.mark.parametrize("error", [TaskCancelled(), asyncio.CancelledError()])
def test_source_cancellation_propagates_without_fabricating_delivery(error):
    downloader = NS(get_messages=AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        asyncio.run(transfer._fetch_media_group(downloader, member()))


def test_more_than_ten_album_members_is_not_a_complete_manifest():
    downloader = NS(get_messages=AsyncMock(return_value=[member(i) for i in range(5, 16)]))
    with pytest.raises(transfer.SourceResolutionError):
        asyncio.run(transfer._fetch_media_group(downloader, member()))


@pytest.mark.parametrize("private", [False, True])
def test_transfer_source_failure_never_sends_or_fabricates_member_failure(private):
    uploader = NS(
        copy_media_group=AsyncMock(), copy_message=AsyncMock(), send_media_group=AsyncMock()
    )
    downloader = NS(get_messages=AsyncMock(side_effect=OSError("unavailable")))
    task = Task(1, "single", 1)
    task.media_sent.add((-100777, 9))  # 另一条已送达事实不受读取异常影响。
    with pytest.raises(transfer.SourceResolutionError):
        asyncio.run(
            transfer.transfer_message(
                uploader,
                downloader,
                member(),
                UserSettings(1),
                "1",
                source_private=private,
                task=task,
            )
        )
    assert task.media_sent == {(-100777, 9)} and not task.media_failed
    uploader.copy_media_group.assert_not_awaited()
    uploader.copy_message.assert_not_awaited()
    uploader.send_media_group.assert_not_awaited()


@pytest.mark.parametrize("batch", [False, True])
def test_extractor_source_error_never_commits_stats_or_history(monkeypatch, batch):
    downloader = NS(get_messages=AsyncMock(side_effect=OSError("source unavailable")))
    uploader = NS(copy_media_group=AsyncMock())
    message = member()
    message._client = downloader
    monkeypatch.setattr(extractor, "fetch_message", AsyncMock(return_value=message))
    monkeypatch.setattr(extractor.clients, "get_user_client", AsyncMock(return_value=downloader))
    monkeypatch.setattr(extractor.clients, "get_upload_bot", AsyncMock(return_value=uploader))
    monkeypatch.setattr(extractor, "load_user_settings", AsyncMock(return_value=UserSettings(1)))
    commit = AsyncMock()
    monkeypatch.setattr(extractor, "record_extract_success", commit)
    status = NS(id=90, chat=NS(id=1), edit=AsyncMock())
    request = NS(from_user=NS(id=1), chat=NS(id=1), reply=AsyncMock(return_value=status))
    task = Task(1, "batch" if batch else "single", 2 if batch else 1)
    ref = MessageLink("-100777", 10, True)
    if batch:
        asyncio.run(extractor.extract_range(request, ref, 2, task))
    else:
        asyncio.run(extractor.extract_single(request, ref, task))
    assert "完整相册" in status.edit.call_args.args[0]
    assert task.success == 0 and not task.media_sent and not task.media_failed
    commit.assert_not_awaited()
    uploader.copy_media_group.assert_not_awaited()
