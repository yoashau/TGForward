"""传输 API、成员状态、话题目标和取消时序。"""

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from pyrogram import Client, enums, raw, utils
from pyrogram.errors import EntityBoundsInvalid, FloodWait, PeerIdInvalid

from tests.transfers.test_routing import _media_message, _text_message
from tgforward.comments import discussion
from tgforward.runtime import tasks
from tgforward.storage.users import UserSettings
from tgforward.telegram import clients
from tgforward.telegram import wait as telegram_wait
from tgforward.transfers import delivery, extractor, progress, transfer
from tgforward.utils.links import MessageLink


def status():
    return NS(id=91, chat=NS(id=1), edit=AsyncMock(), delete=AsyncMock())


class StrictClient:
    """绑定已安装 Client 的真实签名，并验证相册数量、Topic/Reply 和 caption。"""

    def __init__(self, fail_member=None):
        self.calls = []
        self.delivered = []
        self.fail_member = fail_member

    def __getattr__(self, method):
        if not method.startswith(("send_", "copy_", "edit_")):
            raise AttributeError(method)
        signature = inspect.signature(getattr(Client, method))

        async def call(*args, **kwargs):
            signature.bind(self, *args, **kwargs)
            for field in ("message_thread_id", "reply_to_message_id"):
                value = kwargs.get(field)
                assert value is None or isinstance(value, int) and value > 0
            assert kwargs.get("caption") is None or isinstance(kwargs["caption"], str)
            if method == "send_media_group":
                assert 2 <= len(args[1]) <= 10
                ids = [int(Path(item.media).stem) for item in args[1]]
                assert all(
                    item.caption is None or isinstance(item.caption, str) for item in args[1]
                )
            elif method == "copy_media_group":
                ids = [args[2], args[2] + 1]
            elif method == "copy_message":
                ids = [args[2]]
            elif method in ("send_message", "edit_message_reply_markup"):
                ids = []
            else:
                ids = [int(Path(args[1]).stem)]
            self.calls.append((method, args, kwargs))
            if self.fail_member in ids:
                raise PeerIdInvalid()
            self.delivered.extend(ids)
            result = [NS(id=i, chat=NS(id=args[0])) for i in ids]
            return result if method.endswith("media_group") else result[0] if result else status()

        return call


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(transfer, "_send_lock", asyncio.Lock())
    monkeypatch.setattr(transfer, "_TRANSFER_SEMAPHORE", asyncio.Semaphore(4))
    monkeypatch.setattr(transfer, "_last_send_at", 0)
    monkeypatch.setattr(transfer, "_MIN_SEND_INTERVAL", 0)
    monkeypatch.setattr(delivery, "_records", delivery.OrderedDict())
    monkeypatch.setattr(tasks, "_tasks", {})
    monkeypatch.setattr(tasks, "_last_finished", {})


def album_io(monkeypatch, tmp_path, sizes, fail_member=None):
    group = [
        _media_message(id=i, media_group_id="album", document=NS(file_size=size))
        for i, size in enumerate(sizes, 1)
    ]

    async def download(_, message, **kwargs):
        path = tmp_path / f"{message.id}.bin"
        path.write_bytes(b"media")
        return str(path)

    monkeypatch.setattr(transfer, "download_media", download)
    monkeypatch.setattr(transfer, "_fetch_media_group", AsyncMock(return_value=group))
    monkeypatch.setattr(transfer.os.path, "getsize", lambda path: sizes[int(Path(path).stem) - 1])
    monkeypatch.setattr(transfer, "LOG_GROUP", -100999)
    premium, uploader = StrictClient(), StrictClient(fail_member)
    monkeypatch.setattr(clients, "premium", premium)
    monkeypatch.setattr(clients, "premium_started", True)
    return group, uploader, premium


def batch_io(monkeypatch, group, uploader):
    async def fetch(*a, message_id=None, **kw):
        return next((m for m in group if m.id == message_id), None)

    monkeypatch.setattr(extractor, "fetch_message", fetch)
    monkeypatch.setattr(transfer, "_fetch_media_group", AsyncMock(return_value=group))
    monkeypatch.setattr(clients, "get_user_client", AsyncMock(return_value=NS()))
    monkeypatch.setattr(clients, "get_upload_bot", AsyncMock(return_value=uploader))
    monkeypatch.setattr(extractor, "load_user_settings", AsyncMock(return_value=UserSettings(1)))
    monkeypatch.setattr(extractor, "_comment_metadata", AsyncMock(return_value=None))
    monkeypatch.setattr(extractor, "_extract_comments", AsyncMock())
    monkeypatch.setattr(extractor, "record_extract_success", AsyncMock())
    monkeypatch.setattr(extractor, "BATCH_DELAY", 0)
    result = status()
    request = NS(from_user=NS(id=1), chat=NS(id=1), reply=AsyncMock(return_value=result))
    return request, result


def test_batch_album_failure_never_counts_following_members_success(monkeypatch, tmp_path):
    group, uploader, _ = album_io(monkeypatch, tmp_path, [1, 1, 1], fail_member=1)
    request, result = batch_io(monkeypatch, group, uploader)
    task = tasks.Task(1, "batch", 3)
    asyncio.run(extractor.extract_range(request, MessageLink("-100777", 1, True), 3, task))
    assert task.success == 0 and task.current == 3
    assert task.media_failed == {(-100777, i) for i in (1, 2, 3)}
    assert not task.media_sent and not uploader.delivered
    assert len(uploader.calls) == 1
    assert "成功 0/3" in result.edit.call_args.args[0]
    assert "失败 3 条" in result.edit.call_args.args[0]


@pytest.mark.parametrize("sizes", [[1, 2, 1], [2, 1, 1, 2, 1], [1, 1, 2, 1, 1], [2, 2]])
def test_mixed_album_keeps_original_order_and_no_singleton_groups(monkeypatch, tmp_path, sizes):
    sizes = [1 if size == 1 else transfer.LARGE_FILE_BYTES + 1 for size in sizes]
    group, uploader, _ = album_io(monkeypatch, tmp_path, sizes)
    task = tasks.Task(1, "batch", len(group))
    asyncio.run(
        transfer.transfer_message(
            uploader,
            NS(),
            group[0],
            UserSettings(1),
            "1",
            source_private=True,
            task=task,
            status=status(),
            media_group=group,
        )
    )
    assert uploader.delivered == list(range(1, len(group) + 1))
    assert task.media_sent == {(-100777, i) for i in uploader.delivered}
    assert not task.media_failed
    assert not list(tmp_path.iterdir())


def test_partial_album_cross_module_counts_and_retry_only_unsent(monkeypatch, tmp_path):
    group, uploader, premium = album_io(
        monkeypatch, tmp_path, [1, transfer.LARGE_FILE_BYTES + 1, 1, 1], 2
    )
    request, result = batch_io(monkeypatch, group, uploader)

    async def check():
        task = tasks.Task(1, "batch", 4)
        await extractor.extract_range(request, MessageLink("-100777", 1, True), 4, task)
        assert uploader.delivered == [1]
        assert task.success == 1 and task.media_sent == {(-100777, 1)}
        assert task.media_failed == {(-100777, 2)}  # 3/4 尚未尝试，不是发送失败。
        assert "失败 1 条" in result.edit.call_args.args[0]
        assert "未发送或已删除 2 条" in result.edit.call_args.args[0]
        uploader.fail_member = None
        retry = tasks.Task(1, "batch", 4)
        await extractor.extract_range(request, MessageLink("-100777", 1, True), 4, retry)
        assert uploader.delivered == [1, 2, 3, 4]
        assert retry.success == 4 and len(retry.media_sent) == 4 and not retry.media_failed
        assert premium.delivered == [2, 2]  # 中转上传可重试；已送达目标的 1 不重发。
        assert not delivery._records
        await delivery.shutdown()

    asyncio.run(check())


def test_discussion_partial_album_counts_same_member_states(monkeypatch, tmp_path):
    group, uploader, _ = album_io(monkeypatch, tmp_path, [1, transfer.LARGE_FILE_BYTES + 1, 1], 2)
    monkeypatch.setattr(discussion, "collect", AsyncMock(return_value=(group, False)))
    task = tasks.Task(1, "comments", 1)
    text = asyncio.run(
        discussion.extract(NS(), uploader, "channel", 1, UserSettings(1), 1, task, status())
    )
    assert "成功 1 条，失败 1 条" in text and "未尝试发送 1 条" in text
    assert task.comments_incomplete and task.media_failed == {(-100777, 2)}


def test_album_delivery_does_not_edit_comment_buttons(monkeypatch, tmp_path):
    group, uploader, _ = album_io(monkeypatch, tmp_path, [1, 1])
    send = AsyncMock(side_effect=RuntimeError("button edit failed"))
    uploader.edit_message_reply_markup = send

    async def check():
        task = tasks.Task(1, "single", 1)
        await transfer.transfer_message(
            uploader,
            NS(),
            group[0],
            UserSettings(1),
            "1",
            source_private=True,
            task=task,
            status=status(),
            media_group=group,
        )
        assert len(task.media_sent) == 2 and not task.media_failed
        send.assert_not_awaited()
        await transfer.transfer_message(
            uploader,
            NS(),
            group[0],
            UserSettings(1),
            "1",
            source_private=True,
            task=tasks.Task(1, "single", 1),
            status=status(),
            media_group=group,
        )
        assert uploader.delivered == [1, 2, 1, 2]
        await delivery.shutdown()

    asyncio.run(check())


@pytest.mark.parametrize("large", [False, True])
def test_private_voice_caption_regular_and_premium(monkeypatch, tmp_path, large):
    group, uploader, premium = album_io(
        monkeypatch, tmp_path, [transfer.LARGE_FILE_BYTES + 1 if large else 1]
    )
    voice = _media_message(id=1, voice=NS(file_size=1), caption="old caption")
    asyncio.run(
        transfer.transfer_message(
            uploader,
            NS(),
            voice,
            UserSettings(1, caption="footer", replacements={"old": "new"}, chat_id="-100123/9"),
            "1",
            source_private=True,
            task=tasks.Task(1, "single", 1),
            status=status(),
            reply_to_message_id=777,
        )
    )
    sent = premium.calls[0] if large else uploader.calls[0]
    assert sent[0] == "send_voice" and sent[2]["caption"] == "new caption\n\nfooter"
    destination_call = uploader.calls[-1]
    assert destination_call[2]["message_thread_id"] == 9
    assert destination_call[2]["reply_to_message_id"] == 777


@pytest.mark.parametrize(
    "kind", ["photo", "video", "audio", "document", "voice", "video_note", "sticker", "animation"]
)
def test_all_regular_media_methods_keep_independent_topic_and_reply(kind):
    uploader = StrictClient()
    m = _media_message(**{kind: NS(file_size=1)})
    asyncio.run(
        transfer._upload_regular(
            uploader,
            m,
            "1.bin",
            "caption",
            {"width": 1, "height": 1, "duration": 1},
            None,
            -100123,
            transfer.Destination(-100123, 9, 777),
            status(),
            tasks.Task(1, "single", 1),
        )
    )
    method, _, kwargs = uploader.calls[0]
    assert method == "send_" + kind
    assert kwargs["message_thread_id"] == 9 and kwargs["reply_to_message_id"] == 777
    assert "reply_markup" not in kwargs


def test_every_text_chunk_keeps_topic_and_reply():
    uploader = StrictClient()
    asyncio.run(
        transfer.transfer_message(
            uploader,
            NS(),
            _text_message("x" * 9000),
            UserSettings(1, chat_id="-100123/9"),
            "1",
            source_private=False,
            reply_to_message_id=777,
        )
    )
    assert len(uploader.calls) == 3
    for _, _, kwargs in uploader.calls:
        assert kwargs["message_thread_id"] == 9 and kwargs["reply_to_message_id"] == 777
    assert "reply_markup" not in uploader.calls[0][2]


def test_markdown_fallback_changes_only_parse_mode():
    send = AsyncMock(side_effect=[EntityBoundsInvalid(), status()])
    asyncio.run(
        transfer.transfer_message(
            NS(send_message=send),
            NS(),
            _text_message("**x**"),
            UserSettings(1, chat_id="-100123/9"),
            "1",
            source_private=False,
            reply_to_message_id=777,
        )
    )
    first, second = send.call_args_list
    assert first.args == second.args
    assert second.kwargs == {**first.kwargs, "parse_mode": enums.ParseMode.DISABLED}
    assert second.kwargs["message_thread_id"] == 9 and second.kwargs["reply_to_message_id"] == 777
    assert "reply_markup" not in second.kwargs


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("uncertain"),
        ConnectionError("disconnect"),
        PeerIdInvalid(),
        RuntimeError("RPC"),
    ],
)
def test_non_parse_errors_never_trigger_second_send(error):
    send = AsyncMock(side_effect=error)
    with pytest.raises((type(error), transfer.TransferError)):
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
    send.assert_awaited_once()


def test_server_accepted_but_client_timed_out_does_not_duplicate():
    accepted = []

    async def send(chat, text, **kwargs):
        accepted.append((chat, text))
        raise TimeoutError("reply was lost after server accepted")

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
    assert accepted == [(1, "hello")]


@pytest.mark.parametrize("private", [False, True])
def test_album_has_no_comment_button_or_companion(monkeypatch, tmp_path, private):
    group, uploader, _ = album_io(monkeypatch, tmp_path, [1, 1])
    asyncio.run(
        transfer.transfer_message(
            uploader,
            NS(),
            group[0],
            UserSettings(1, chat_id="-100123/9"),
            "1",
            source_private=private,
            status=status(),
            task=tasks.Task(1, "single", 1),
            media_group=group,
            reply_to_message_id=777,
        )
    )
    assert not any(method == "edit_message_reply_markup" for method, _, _ in uploader.calls)
    assert not any(method == "send_message" for method, _, _ in uploader.calls)
    for _, _, kwargs in uploader.calls:
        assert kwargs["message_thread_id"] == 9 and kwargs["reply_to_message_id"] == 777
    assert "reply_markup" not in uploader.calls[-1][2]


def virtual_clock(monkeypatch):
    now, sleeps = [0.0], []
    original_sleep = asyncio.sleep
    monkeypatch.setattr(telegram_wait.time, "monotonic", lambda: now[0])

    async def sleep(seconds):
        sleeps.append(seconds)
        if seconds > telegram_wait.HEARTBEAT_INTERVAL:
            # watchdog 与工作共享同一时钟，不能每次调度都额外推进 30 秒；
            # Python 3.10 wait_for 调度更多，否则会制造并不存在的 300 秒停滞。
            deadline = now[0] + seconds
            while now[0] < deadline:
                await original_sleep(0)
        else:
            now[0] += seconds
            await original_sleep(0)

    monkeypatch.setattr(telegram_wait.asyncio, "sleep", sleep)
    return now, sleeps


def test_600_second_send_floodwait_keeps_heartbeat_and_reenters_gate(monkeypatch):
    now, sleeps = virtual_clock(monkeypatch)
    task = tasks.Task(1, "single", 1)
    task.touch = Mock(wraps=task.touch)
    send = AsyncMock(side_effect=[FloodWait(600), FloodWait(1), "ok"])
    assert asyncio.run(transfer._send(send, task)) == "ok"
    assert now[0] >= 603 and max(sleeps) <= 5
    assert task.touch.call_count >= 120 and task.last_activity >= 603
    assert send.await_count == 3 and not task.timed_out


@pytest.mark.parametrize("blocked_by", ["flood", "rpc"])
def test_one_users_wait_does_not_hold_global_send_lock(monkeypatch, blocked_by):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked():
            entered.set()
            await release.wait()
            return "first"

        original_wait = transfer.heartbeat_sleep

        async def wait(seconds, *args, **kwargs):
            if seconds >= 600:
                await blocked()
            else:
                await original_wait(seconds, *args, **kwargs)

        monkeypatch.setattr(transfer, "heartbeat_sleep", wait)
        send = (
            AsyncMock(side_effect=[FloodWait(600), "first"]) if blocked_by == "flood" else blocked
        )
        first = asyncio.create_task(transfer._send(send, tasks.Task(1, "single", 1)))
        await entered.wait()
        assert not transfer._send_lock.locked()
        second = await asyncio.wait_for(
            transfer._send(AsyncMock(return_value="second"), tasks.Task(2, "single", 1)), 0.3
        )
        assert second == "second" and not first.done()
        release.set()
        assert await first == "first"

    asyncio.run(check())


def test_resource_queue_keeps_heartbeat_and_cancel_releases_no_unowned_slot(monkeypatch):
    monkeypatch.setattr(telegram_wait, "HEARTBEAT_INTERVAL", 0.01)

    async def check():
        semaphore = asyncio.Semaphore(0)
        task = tasks.Task(1, "single", 1)
        pulses = asyncio.Event()
        original_touch = task.touch
        count = 0

        def touch(stage=None):
            nonlocal count
            original_touch(stage)
            count += 1
            if count >= 4:
                pulses.set()

        task.touch = touch

        async def work():
            async with telegram_wait.acquire(semaphore, task):
                return "acquired"

        job = asyncio.create_task(work())
        await asyncio.wait_for(pulses.wait(), 1)
        assert count >= 4 and not job.done()
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert semaphore._value == 0
        semaphore.release()
        assert await work() == "acquired" and semaphore._value == 1

    asyncio.run(check())


@pytest.mark.parametrize("phase", ["inspect", "root", "page"])
def test_comment_reads_retry_600_second_floodwait_at_every_stage(monkeypatch, phase):
    now, sleeps = virtual_clock(monkeypatch)
    task = tasks.Task(1, "comments", 1)
    counts = {"inspect": 0, "root": 0, "page": 0}

    async def invoke(request):
        stage = (
            "inspect"
            if isinstance(request, raw.functions.channels.GetMessages)
            else "root"
            if isinstance(request, raw.functions.messages.GetDiscussionMessage)
            else "page"
        )
        counts[stage] += 1
        if stage == phase and counts[stage] == 1:
            raise FloodWait(600)
        if stage == "inspect":
            return NS(messages=[NS(id=1, replies=NS(comments=True, channel_id=7))])
        if stage == "root":
            return NS(
                messages=[NS(id=10, peer_id=raw.types.PeerChannel(channel_id=7))],
                chats=[NS(id=7, megagroup=True, access_hash=8)],
            )
        return NS(messages=[NS(id=11)], users=[], chats=[])

    client = NS(
        get_messages=AsyncMock(return_value=NS(id=1, chat=NS(type=enums.ChatType.CHANNEL))),
        resolve_peer=AsyncMock(
            return_value=raw.types.InputPeerChannel(channel_id=5, access_hash=6)
        ),
        invoke=invoke,
    )
    monkeypatch.setattr(
        discussion.Message,
        "_parse",
        AsyncMock(
            return_value=NS(
                id=11,
                chat=NS(id=utils.get_peer_id(raw.types.PeerChannel(channel_id=7))),
                empty=False,
            )
        ),
    )
    messages, truncated = asyncio.run(discussion.collect(client, "channel", 1, task))
    assert [m.id for m in messages] == [11] and not truncated
    assert counts[phase] == 2 and now[0] >= 601 and max(sleeps) <= 5
    assert task.last_activity >= 600 and not task.timed_out


def test_comment_cancellation_is_not_relabelled_as_transfer_error():
    async def read():
        raise tasks.TaskCancelled()

    with pytest.raises(tasks.TaskCancelled):
        asyncio.run(discussion._read_stage(read, "read"))


def test_progress_throttle_is_callback_local_on_failure_and_cancellation():
    # 每个回调独立维护节流状态。
    assert not hasattr(progress, "_throttle")

    async def check():
        task = tasks.Task(1, "single", 1)
        msg = status()
        first = progress.make_progress(NS(), 1, 91, task, status_message=msg)
        await first(10, 100)
        await first(11, 100)
        assert msg.edit.await_count == 1
        second = progress.make_progress(NS(), 1, 91, task, status_message=msg)
        await second(10, 100)
        assert msg.edit.await_count == 2  # 新上传阶段不会被旧阶段节流状态压制。

    asyncio.run(check())


@pytest.mark.parametrize(
    "captions,expected",
    [
        (["", None], ["", "keep"]),
        ([None, ""], ["remove", ""]),
        ("", ["", ""]),
        (None, ["remove", "keep"]),
    ],
)
def test_real_pyrofork_copy_album_preserves_empty_vs_none(monkeypatch, captions, expected):
    from pyrogram.methods.messages.copy_media_group import CopyMediaGroup

    from tgforward.telegram.compat import install

    install()
    source = [
        NS(photo=NS(file_id="photo"), caption=text, has_media_spoiler=False)
        for text in ["remove", "keep"]
    ]

    async def parse(text):
        return {"message": str(text or ""), "entities": []}

    client = NS(
        get_media_group=AsyncMock(return_value=source),
        parser=NS(parse=parse),
        rnd_id=lambda: 1,
        resolve_peer=AsyncMock(return_value=raw.types.InputPeerSelf()),
        invoke=AsyncMock(return_value=NS(updates=[], users=[], chats=[])),
    )
    monkeypatch.setattr(
        utils, "get_input_media_from_file_id", lambda **kwargs: raw.types.InputMediaEmpty()
    )
    monkeypatch.setattr(
        utils, "parse_text_entities", AsyncMock(return_value={"message": "", "entities": []})
    )
    asyncio.run(
        CopyMediaGroup.copy_media_group(
            client,
            -100123,
            -100777,
            1,
            captions=captions,
            message_thread_id=9,
            reply_to_message_id=777,
        )
    )
    request = client.invoke.call_args.args[0]
    assert [item.message for item in request.multi_media] == expected
    assert request.reply_to.message_thread_id == 9 and request.reply_to.reply_to_message_id == 777
    assert client.invoke.await_count == 1


def test_strict_fake_rejects_single_item_album():
    from pyrogram.types import InputMediaPhoto

    with pytest.raises(AssertionError):
        asyncio.run(StrictClient().send_media_group(1, [InputMediaPhoto("1.jpg")]))


def test_confirmed_send_is_committed_even_if_cancel_requested_with_response(monkeypatch, tmp_path):
    group, uploader, _ = album_io(monkeypatch, tmp_path, [1, 1])
    task = tasks.Task(1, "single", 1)
    original = uploader.send_media_group

    async def send(*args, **kwargs):
        result = await original(*args, **kwargs)
        task.cancel_requested = True
        return result

    uploader.send_media_group = send
    asyncio.run(
        transfer.transfer_message(
            uploader,
            NS(),
            group[0],
            UserSettings(1),
            "1",
            source_private=True,
            task=task,
            status=status(),
            media_group=group,
        )
    )
    assert len(task.media_sent) == 2 and not task.media_failed


@pytest.mark.parametrize("cancelled", [False, True])
def test_aborted_upload_without_result_is_never_success(monkeypatch, tmp_path, cancelled):
    group, uploader, _ = album_io(monkeypatch, tmp_path, [1, 1])
    task = tasks.Task(1, "single", 1)

    async def send(*args, **kwargs):
        task.cancel_requested = cancelled
        return None

    uploader.send_media_group = send
    with pytest.raises(tasks.TaskCancelled if cancelled else transfer.TransferError):
        asyncio.run(
            transfer.transfer_message(
                uploader,
                NS(),
                group[0],
                UserSettings(1),
                "1",
                source_private=True,
                task=task,
                status=status(),
                media_group=group,
            )
        )
    assert not task.media_sent


def test_send_gate_retries_peer_errors_with_same_translation(monkeypatch):
    monkeypatch.setattr(transfer, "heartbeat_sleep", AsyncMock())
    send = AsyncMock(side_effect=[FloodWait(600), PeerIdInvalid()])
    with pytest.raises(transfer.TransferError, match="目标聊天不可达"):
        asyncio.run(transfer._send(send, tasks.Task(1, "single", 1)))
    assert send.await_count == 2


def test_resume_journal_expires_without_traffic_and_is_bounded(monkeypatch):
    monkeypatch.setattr(delivery, "TTL", 0.02)
    monkeypatch.setattr(delivery, "CAPACITY", 3)

    async def check():
        for i in range(8):
            with pytest.raises(RuntimeError), delivery.attempt(str(i)):
                delivery.commit({(1, i)})
                raise RuntimeError("partial")
        assert len(delivery._records) == 3
        await asyncio.sleep(0.05)
        assert not delivery._records
        await delivery.shutdown()

    asyncio.run(check())


def test_resume_journal_key_separates_settings_target_reply_and_user():
    uploader = StrictClient()
    m = _media_message(media_group_id="g", photo=NS(file_size=1))
    key = delivery.key_for(uploader, m, [m], UserSettings(1), "-100123/9", None)
    for settings, target, reply in [
        (UserSettings(2), "-100123/9", None),
        (UserSettings(1, caption="new"), "-100123/9", None),
        (UserSettings(1), "-100123/10", None),
        (UserSettings(1), "-100123/9", 777),
    ]:
        assert delivery.key_for(uploader, m, [m], settings, target, reply) != key


def test_same_task_album_retry_does_not_repeat_committed_members(monkeypatch, tmp_path):
    group, uploader, _ = album_io(monkeypatch, tmp_path, [1, transfer.LARGE_FILE_BYTES + 1, 1], 2)

    async def check():
        task = tasks.Task(1, "batch", 3)
        with pytest.raises(transfer.TransferError):
            await transfer.transfer_message(
                uploader,
                NS(),
                group[0],
                UserSettings(1),
                "1",
                source_private=True,
                task=task,
                status=status(),
                media_group=group,
            )
        uploader.fail_member = None
        await transfer.transfer_message(
            uploader,
            NS(),
            group[0],
            UserSettings(1),
            "1",
            source_private=True,
            task=task,
            status=status(),
            media_group=group,
        )
        assert uploader.delivered == [1, 2, 3]
        assert len(task.media_sent) == 3 and not task.media_failed
        await delivery.shutdown()

    asyncio.run(check())


def test_resume_does_not_reuse_another_destination_in_same_task(monkeypatch, tmp_path):
    group, uploader, _ = album_io(monkeypatch, tmp_path, [1, transfer.LARGE_FILE_BYTES + 1, 1], 2)

    async def check():
        task = tasks.Task(1, "batch", 3)
        with pytest.raises(transfer.TransferError):
            await transfer.transfer_message(
                uploader,
                NS(),
                group[0],
                UserSettings(1),
                "1",
                source_private=True,
                task=task,
                status=status(),
                media_group=group,
            )
        uploader.fail_member = None
        await transfer.transfer_message(
            uploader,
            NS(),
            group[0],
            UserSettings(1, chat_id="-100123/9"),
            "1",
            source_private=True,
            task=task,
            status=status(),
            media_group=group,
        )
        assert uploader.delivered == [1, 1, 2, 3]
        await delivery.shutdown()

    asyncio.run(check())


def test_600_second_floodwait_survives_actual_task_watchdog(monkeypatch):
    now, _ = virtual_clock(monkeypatch)
    monkeypatch.setattr(tasks, "TASK_STALL_TIMEOUT", 300)

    async def check():
        task = tasks.register(1, "single", 1)
        send = AsyncMock(side_effect=[FloodWait(600), status()])

        async def work():
            await transfer._send(send, task)
            task.advance(success=True)

        tasks.launch(task, work, AsyncMock())
        await task.runner
        assert task.success == 1 and send.await_count == 2
        assert now[0] >= 600 and not task.timed_out and not task.cancelled
        assert not tasks.is_active(1)

    asyncio.run(check())


@pytest.mark.parametrize(
    "kind", ["photo", "video", "animation", "audio", "voice", "document", "sticker", "video_note"]
)
@pytest.mark.parametrize("private", [False, True])
def test_comment_media_uses_real_media_send_path(monkeypatch, tmp_path, kind, private):
    member = _media_message(id=1, **{kind: NS(file_size=5)})
    if not private:
        member.chat.username = "public_comments"
    uploader = StrictClient()
    downloads = []

    async def download(*args, **kwargs):
        path = tmp_path / "1.bin"
        path.write_bytes(b"media")
        downloads.append(path)
        return str(path)

    monkeypatch.setattr(transfer, "download_media", download)
    monkeypatch.setattr(transfer, "get_video_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(transfer, "screenshot", AsyncMock(return_value=None))
    monkeypatch.setattr(transfer, "custom_thumb_path", lambda _: None)
    monkeypatch.setattr(discussion, "collect", AsyncMock(return_value=([member], False)))
    task = tasks.Task(1, "comments", 1)
    summary = asyncio.run(
        discussion.extract(NS(), uploader, "source", 1, UserSettings(1), 1, task, status())
    )
    assert "成功 1 条，失败 0 条" in summary
    assert [call[0] for call in uploader.calls] == [f"send_{kind}" if private else "copy_message"]
    assert len(downloads) == int(private)
    assert task.media_sent == {(-100777, 1)}
    assert not task.comments_incomplete


def test_comment_copy_access_error_downloads_media_instead_of_placeholder(monkeypatch, tmp_path):
    from pyrogram.errors import ChannelPrivate

    group, uploader, _ = album_io(monkeypatch, tmp_path, [1, 1])
    for member in group:
        member.chat.username = "public_comments"
    uploader.copy_media_group = AsyncMock(side_effect=ChannelPrivate())
    monkeypatch.setattr(discussion, "collect", AsyncMock(return_value=(group, False)))
    task = tasks.Task(1, "comments", 1)
    summary = asyncio.run(
        discussion.extract(NS(), uploader, "source", 1, UserSettings(1), 1, task, status())
    )
    assert "成功 2 条，失败 0 条" in summary
    assert [call[0] for call in uploader.calls] == ["send_media_group"]
    assert not task.media_failed and len(task.media_sent) == 2


@pytest.mark.parametrize("error", [RuntimeError("network"), TimeoutError()])
def test_comment_unknown_copy_error_is_not_retried_as_download(monkeypatch, error):
    member = _media_message(photo=NS(file_size=1))
    member.chat.username = "public_comments"
    uploader = NS(copy_message=AsyncMock(side_effect=error))
    download = AsyncMock()
    monkeypatch.setattr(transfer, "download_media", download)
    monkeypatch.setattr(discussion, "collect", AsyncMock(return_value=([member], False)))
    task = tasks.Task(1, "comments", 1)
    asyncio.run(discussion.extract(NS(), uploader, "source", 1, UserSettings(1), 1, task, status()))
    download.assert_not_awaited()
    assert task.comments_incomplete


def test_album_unsupported_buttons_need_no_companion(monkeypatch, tmp_path):
    from pyrogram.errors import MessageIdInvalid

    group, uploader, _ = album_io(monkeypatch, tmp_path, [1, 1])
    uploader.edit_message_reply_markup = AsyncMock(side_effect=MessageIdInvalid())
    outcome = asyncio.run(
        transfer.transfer_message(
            uploader,
            NS(),
            group[0],
            UserSettings(1),
            "1",
            source_private=False,
            task=tasks.Task(1, "single", 1),
            status=status(),
            media_group=group,
        )
    )
    assert [call[0] for call in uploader.calls] == ["copy_media_group"]
    assert "reply_markup" not in uploader.calls[-1][2]
    uploader.edit_message_reply_markup.assert_not_awaited()
    assert outcome.sent_message.id == 1
