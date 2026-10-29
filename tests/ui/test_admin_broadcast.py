"""管理员广播必须按字面发送，并覆盖始终有权限的管理员。"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from pyrogram import enums
from pyrogram.errors import FloodWait

from tgforward.handlers import admin
from tgforward.telegram import clients
from tgforward.ui.i18n import language_context


def test_broadcast_is_literal_includes_owners_and_retries_flood(monkeypatch):
    sent = []

    async def send_message(user_id, text, **kwargs):
        sent.append((user_id, text, kwargs))
        if len(sent) == 1:
            raise FloodWait(2)

    async def run():
        status = NS(edit=AsyncMock())
        message = NS(
            from_user=NS(id=1),
            text="/broadcast hello `world`",
            reply=AsyncMock(return_value=status),
        )
        monkeypatch.setattr(admin, "list_whitelisted_ids", AsyncMock(return_value=[1, 9]))
        monkeypatch.setattr("tgforward.telegram.wait.heartbeat_sleep", AsyncMock())
        monkeypatch.setattr(
            "tgforward.ui.i18n.user_language", AsyncMock(return_value="en")
        )
        await admin.broadcast_handler(NS(send_message=send_message), message)
        return status

    status = asyncio.run(run())
    assert [item[0] for item in sent] == [1, 1, 9]
    assert {item[1] for item in sent} == {"hello `world`"}
    assert all(item[2]["parse_mode"] == enums.ParseMode.DISABLED for item in sent)
    assert "2 succeeded, 0 failed" in status.edit.call_args.args[0]


def test_broadcast_rejects_text_over_telegram_limit(monkeypatch):
    send = AsyncMock()
    message = NS(
        from_user=NS(id=1),
        text="/broadcast " + ("字" * 4097),
        reply=AsyncMock(),
    )

    async def run():
        monkeypatch.setattr(admin, "list_whitelisted_ids", AsyncMock())
        monkeypatch.setattr(
            "tgforward.ui.i18n.user_language", AsyncMock(return_value="en")
        )
        await admin.broadcast_handler(NS(send_message=send), message)

    asyncio.run(run())
    send.assert_not_awaited()
    admin.list_whitelisted_ids.assert_not_awaited()
    assert "4096" in message.reply.call_args.args[0]


def test_uptime_follows_interface_language(monkeypatch):
    monkeypatch.setattr(clients.time, "monotonic", lambda: clients._STARTED_AT + 3661)
    with language_context("en"):
        assert clients.uptime_text() == "1h 1m 1s"
    with language_context("zh"):
        assert clients.uptime_text() == "1小时1分1秒"


def test_broadcast_paces_with_transfers_and_continues_after_one_failure(monkeypatch):
    sent = []

    async def send_message(user_id, text, **kwargs):
        sent.append(user_id)
        if user_id == 9:
            raise RuntimeError("blocked")

    slots = []

    async def reserve(task=None, interval=None):
        slots.append(interval)
        task.check_cancel()

    async def run():
        status = NS(edit=AsyncMock())
        message = NS(
            from_user=NS(id=1),
            text="/broadcast hello",
            reply=AsyncMock(return_value=status),
        )
        monkeypatch.setattr(admin, "list_whitelisted_ids", AsyncMock(return_value=[9]))
        monkeypatch.setattr(admin.transfer, "reserve_send_slot", reserve)
        monkeypatch.setattr("tgforward.ui.i18n.user_language", AsyncMock(return_value="en"))
        await admin.broadcast_handler(NS(send_message=send_message), message)
        return status

    status = asyncio.run(run())
    assert sent == [1, 9]
    assert slots == [admin.BROADCAST_INTERVAL, admin.BROADCAST_INTERVAL]
    assert "1 succeeded, 1 failed" in status.edit.call_args.args[0]
    assert 1 not in admin._broadcast_stops


def test_broadcast_cancel_stops_before_remaining_users(monkeypatch):
    sent = []

    async def send_message(user_id, text, **kwargs):
        sent.append(user_id)
        admin.stop_broadcast(1)

    async def reserve(task=None, interval=None):
        return None

    async def run():
        status = NS(edit=AsyncMock())
        message = NS(
            from_user=NS(id=1),
            text="/broadcast hello",
            reply=AsyncMock(return_value=status),
        )
        monkeypatch.setattr(admin, "list_whitelisted_ids", AsyncMock(return_value=[9, 10]))
        monkeypatch.setattr(admin.transfer, "reserve_send_slot", reserve)
        monkeypatch.setattr("tgforward.ui.i18n.user_language", AsyncMock(return_value="en"))
        await admin.broadcast_handler(NS(send_message=send_message), message)
        return status

    status = asyncio.run(run())
    assert sent == [1]
    assert "stopped" in status.edit.call_args.args[0].lower()
    assert "2 not sent" in status.edit.call_args.args[0]
    assert 1 not in admin._broadcast_stops


def test_cancel_command_stops_running_broadcast(monkeypatch):
    from tgforward.handlers import cancel
    from tgforward.ui.i18n import language_context

    admin._broadcast_stops[1] = asyncio.Event()
    monkeypatch.setattr(cancel.tasks, "get", lambda uid: None)
    monkeypatch.setattr(cancel.tasks, "request_cancel", lambda uid: False)
    monkeypatch.setattr(cancel.dialogue, "clear", AsyncMock(return_value=False))
    message = NS(from_user=NS(id=1), reply=AsyncMock())

    async def run():
        with language_context("en"):
            await cancel.cancel_command.__wrapped__(None, message)

    asyncio.run(run())
    assert admin._broadcast_stops[1].is_set()
    assert "broadcast" in message.reply.call_args.args[0]
    admin._broadcast_stops.pop(1, None)
