import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pyrogram.errors import EntityBoundsInvalid, FloodWait, PeerIdInvalid

from tests.transfers.test_routing import _text_message
from tgforward.runtime.tasks import CancelReason, Task, TaskCancelled
from tgforward.storage.users import UserSettings
from tgforward.transfers import rpc, transfer
from tgforward.transfers.results import DeliveryPart, MessageDeliveryState


def state():
    delivery = MessageDeliveryState()
    delivery.seal([DeliveryPart("a", 1), DeliveryPart("b", 2)])
    return delivery


@pytest.mark.parametrize("error", [TimeoutError(), OSError(), asyncio.CancelledError()])
def test_inflight_unknown_never_marks_unattempted_parts_failed(error):
    delivery = state()
    with pytest.raises(type(error)):
        asyncio.run(rpc.execute(AsyncMock(side_effect=error), None, delivery, ["a"]))
    assert delivery.uncertain == {"a"}
    assert delivery.not_attempted == {"b"} and not delivery.confirmed_failed


@pytest.mark.parametrize("error", [FloodWait(1), EntityBoundsInvalid()])
def test_safe_rejection_stays_retryable_until_success(error):
    async def run():
        delivery = state()
        with pytest.raises(type(error)):
            await rpc.execute(AsyncMock(side_effect=error), None, delivery, ["a"])
        assert delivery.attempts["a"] == "retry_wait" and not delivery.confirmed_failed
        await rpc.execute(AsyncMock(return_value=NS(id=1)), None, delivery, ["a"])
        assert delivery.confirmed_delivered == {"a"} and delivery.attempt_counts["a"] == 2

    asyncio.run(run())


def test_explicit_server_rejection_is_failed_not_uncertain():
    delivery = state()
    with pytest.raises(PeerIdInvalid):
        asyncio.run(rpc.execute(AsyncMock(side_effect=PeerIdInvalid()), None, delivery, ["a"]))
    assert delivery.confirmed_failed == {"a"} and not delivery.uncertain
    assert delivery.not_attempted == {"b"}


@pytest.mark.parametrize("returned", [[], [NS(id=1)], [NS(id=1), NS(id=1)], None])
def test_batch_invalid_return_never_confirms_entire_group(returned):
    delivery = state()
    asyncio.run(
        rpc.execute(AsyncMock(return_value=returned), None, delivery, ["a", "b"], batch=True)
    )
    assert delivery.uncertain == {"a", "b"} and not delivery.confirmed_delivered


def test_text_chunks_commit_before_cooperative_stop(monkeypatch):
    monkeypatch.setattr(transfer, "_MIN_SEND_INTERVAL", 0)
    monkeypatch.setattr(transfer, "_send_lock", asyncio.Lock())
    task = Task(921, "single", 1)

    async def send(*args, **kwargs):
        task.set_cancel_reason(CancelReason.USER)
        return NS(id=1)

    with pytest.raises(TaskCancelled):
        asyncio.run(
            transfer.transfer_message(
                NS(send_message=send),
                NS(),
                _text_message("a" * 9000),
                UserSettings(921),
                "921",
                source_private=False,
                task=task,
            )
        )
    result = task.units[0].message_results[0]
    assert result.delivery.confirmed_delivered == {"text:0"}
    assert result.delivery.not_attempted == {"text:1", "text:2"}
    assert result.outcome == "incomplete"


def test_successful_text_uses_complete_source_and_delivery_model(monkeypatch):
    monkeypatch.setattr(transfer, "_MIN_SEND_INTERVAL", 0)
    monkeypatch.setattr(transfer, "_send_lock", asyncio.Lock())
    result = asyncio.run(
        transfer.transfer_message(
            NS(send_message=AsyncMock(return_value=NS(id=1))),
            NS(),
            _text_message("a" * 9000),
            UserSettings(921),
            "921",
            source_private=False,
        )
    )
    assert result.outcome == "success" and result.source_resolution == "complete"
    assert result.delivery.confirmed_delivered == {"text:0", "text:1", "text:2"}


def test_comment_album_source_failure_does_not_send_known_subset(monkeypatch):
    from tests.transfers.test_routing import _media_message
    from tgforward.comments import discussion

    message = _media_message(id=1, media_group_id="g", photo=NS(file_size=1))
    reader = NS(get_messages=AsyncMock(side_effect=OSError("manifest unavailable")))
    sender = NS(copy_media_group=AsyncMock(), send_media_group=AsyncMock())
    monkeypatch.setattr(discussion, "collect", AsyncMock(return_value=([message], False)))
    task = Task(921, "comments", 1)
    asyncio.run(discussion.extract(reader, sender, "c", 1, UserSettings(921), 921, task, None))
    assert task.comment_result.last_error and not task.comment_result.empty
    assert task.comment_result.failed == task.comment_result.success == 0
    sender.copy_media_group.assert_not_awaited()
    sender.send_media_group.assert_not_awaited()
    result = task.units[-1].message_results[0]
    assert result.source_resolution == "failed" and result.outcome == "incomplete"
    assert not result.delivery.uncertain and not result.delivery.confirmed_failed


def test_media_network_unknown_and_server_rejection_are_distinct(monkeypatch):
    from tests.transfers.test_routing import _media_message

    monkeypatch.setattr(transfer, "_MIN_SEND_INTERVAL", 0)
    for error, expected in [(TimeoutError(), "uncertain"), (PeerIdInvalid(), "failed")]:
        monkeypatch.setattr(transfer, "_send_lock", asyncio.Lock())
        task = Task(921, "single", 1)
        sender = NS(copy_message=AsyncMock(side_effect=error))
        with pytest.raises(transfer.TransferError):
            asyncio.run(
                transfer.transfer_message(
                    sender,
                    NS(),
                    _media_message(photo=NS(file_size=1)),
                    UserSettings(921),
                    "921",
                    source_private=False,
                    task=task,
                )
            )
        result = task.units[0].message_results[0]
        assert result.source_resolution == "complete" and result.outcome == expected
        assert (
            len(
                result.delivery.uncertain
                if expected == "uncertain"
                else result.delivery.confirmed_failed
            )
            == 1
        )


def test_staging_unknown_does_not_mark_final_delivery_unknown(monkeypatch):
    from tests.transfers.test_routing import _media_message
    from tgforward.telegram import clients
    from tgforward.transfers.results import MessageResult

    monkeypatch.setattr(transfer, "_MIN_SEND_INTERVAL", 0)
    monkeypatch.setattr(transfer, "_send_lock", asyncio.Lock())
    monkeypatch.setattr(transfer, "LOG_GROUP", -100999)
    monkeypatch.setattr(clients, "premium_started", True)
    monkeypatch.setattr(clients, "premium", NS(send_photo=AsyncMock(side_effect=TimeoutError())))
    result = MessageResult("id", "key")
    result.resolve_source([1], [DeliveryPart("media:1", 1)])
    uploader = NS(copy_message=AsyncMock())

    async def run():
        token = transfer._current_result.set(result)
        try:
            with pytest.raises(TimeoutError):
                await transfer._upload_large(
                    uploader,
                    _media_message(id=1, photo=NS(file_size=1)),
                    "x",
                    None,
                    None,
                    None,
                    921,
                    None,
                    NS(id=2, chat=NS(id=921)),
                    Task(921, "single", 1),
                )
        finally:
            transfer._current_result.reset(token)

    asyncio.run(run())
    assert result.delivery.not_attempted == {"media:1"}
    assert not result.delivery.uncertain and result.delivery.attempt_counts["media:1"] == 0
    uploader.copy_message.assert_not_awaited()


def test_source_success_commits_before_comment_failure(sqlite_store, monkeypatch):
    from tgforward.storage import users
    from tgforward.transfers import extractor
    from tgforward.utils.links import MessageLink

    async def run():
        uid = 921
        await users.set_field(uid, "caption", "")
        source = _text_message("hello")
        sender = NS(send_message=AsyncMock(return_value=NS(id=1)))
        monkeypatch.setattr(extractor, "fetch_message", AsyncMock(return_value=source))
        monkeypatch.setattr(extractor.clients, "get_user_client", AsyncMock(return_value=NS()))
        monkeypatch.setattr(extractor.clients, "get_upload_bot", AsyncMock(return_value=sender))
        monkeypatch.setattr(
            extractor,
            "load_user_settings",
            AsyncMock(return_value=UserSettings(uid, auto_comments=True)),
        )
        from tgforward.ui.keyboards import CommentAction

        monkeypatch.setattr(
            extractor, "_comment_metadata", AsyncMock(return_value=CommentAction("s", 55, 1))
        )

        async def comments(*args):
            doc = await users.get_user(uid)
            assert doc["stats"]["extracts"] == 1 and len(doc["history"]) == 1
            raise ValueError("discussion unavailable")

        monkeypatch.setattr(extractor.discussion, "extract", comments)
        status = NS(id=8, chat=NS(id=uid), edit=AsyncMock())
        request = NS(chat=NS(id=uid), from_user=NS(id=uid), reply=AsyncMock(return_value=status))
        task = Task(uid, "single", 1)
        await extractor.extract_single(request, MessageLink("s", 55, False), task)
        result = task.units[0].message_results[0]
        assert result.outcome == "success" and task.comment_result.last_error
        markup = status.edit.call_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].callback_data.endswith(":success")
        assert markup.inline_keyboard[1][0].callback_data.startswith("cmt:")
        assert task.status is None and task.active_unit is None
        assert task.units[0].request_current == 1

    asyncio.run(run())


def test_comment_long_text_projects_partial_as_one_source(monkeypatch):
    from tgforward.comments import discussion

    monkeypatch.setattr(transfer, "_MIN_SEND_INTERVAL", 0)
    monkeypatch.setattr(transfer, "_send_lock", asyncio.Lock())
    source = _text_message("x" * 9000)
    monkeypatch.setattr(discussion, "collect", AsyncMock(return_value=([source], False)))
    sender = NS(send_message=AsyncMock(side_effect=[NS(id=1), NS(id=2), PeerIdInvalid()]))
    task = Task(921, "comments", 1)
    asyncio.run(discussion.extract(NS(), sender, "s", 55, UserSettings(921), 921, task, None))
    comments = task.comment_result
    assert comments.partial == 1 and comments.failed == comments.success == comments.uncertain == 0
    result = task.units[-1].message_results[0]
    assert result.delivery.confirmed_delivered == {"text:0", "text:1"}
    assert result.delivery.confirmed_failed == {"text:2"}


def test_comment_copy_fallback_uses_same_retryable_attempt(monkeypatch, tmp_path):
    from pyrogram.errors import ChannelPrivate

    from tests.transfers.test_delivery import album_io, status
    from tgforward.comments import discussion

    group, uploader, _ = album_io(monkeypatch, tmp_path, [1, 1])
    for item in group:
        item.chat.username = "public_comments"
    uploader.copy_media_group = AsyncMock(side_effect=ChannelPrivate())
    monkeypatch.setattr(discussion, "collect", AsyncMock(return_value=(group, False)))
    monkeypatch.setattr(transfer, "_MIN_SEND_INTERVAL", 0)
    monkeypatch.setattr(transfer, "_send_lock", asyncio.Lock())
    task = Task(921, "comments", 1)
    asyncio.run(discussion.extract(NS(), uploader, "s", 1, UserSettings(921), 921, task, status()))
    result = task.units[-1].message_results[0]
    assert result.outcome == "success"
    assert set(result.delivery.attempt_counts.values()) == {2}
    assert not result.delivery.confirmed_failed and not result.delivery.uncertain
    assert task.comment_result.success == 2
