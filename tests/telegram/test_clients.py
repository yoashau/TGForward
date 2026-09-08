"""生命周期 single-flight、凭据删除竞争、启动失败与取消回滚。"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from tgforward.runtime import lifecycle
from tgforward.storage import users
from tgforward.telegram import clients


class Client:
    def __init__(self, mode="ok", entered=None, proceed=None):
        self.is_connected = self.is_initialized = False
        self.mode, self.entered, self.proceed = mode, entered, proceed
        self.starts = self.stops = self.disconnects = 0

    async def start(self):
        self.starts += 1
        self.is_connected = True
        if self.entered:
            self.entered.set()
        if self.proceed:
            await self.proceed.wait()
        if self.mode == "connected":
            raise RuntimeError("get_me failed")
        self.is_initialized = True
        if self.mode == "initialized":
            raise RuntimeError("initialize failed")
        return self

    async def stop(self):
        self.stops += 1
        self.is_initialized = self.is_connected = False

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    for name, value in [
        ("_user_clients", {}),
        ("_helper_bots", {}),
        ("_unclosed", {}),
        ("_closing", False),
        ("_dialogs_cached", set()),
        ("premium_started", False),
    ]:
        monkeypatch.setattr(clients, name, value)
    monkeypatch.setattr(lifecycle, "_locks", {})
    monkeypatch.setattr(lifecycle, "_owners", {})
    monkeypatch.setattr(lifecycle, "revoked", set())


@pytest.mark.parametrize("kind", ["user", "helper"])
def test_single_flight(kind, monkeypatch):
    made = []

    def factory(*args, **kwargs):
        c = Client()
        made.append(c)
        return c

    monkeypatch.setattr(clients, "_client", factory)
    secret = AsyncMock(return_value="credential")
    monkeypatch.setattr(users, "get_session" if kind == "user" else "get_helper_token", secret)
    getter = clients.get_user_client if kind == "user" else clients.get_helper_bot

    async def check():
        result = await asyncio.gather(*(getter(1) for _ in range(12)))
        assert len(made) == 1 and made[0].starts == 1
        assert all(c is made[0] for c in result)
        secret.assert_awaited_once()

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["user", "helper"])
def test_credential_delete_waits_for_creation_then_cleans_cache(kind, monkeypatch):
    async def check():
        entered, proceed = asyncio.Event(), asyncio.Event()
        c = Client(entered=entered, proceed=proceed)
        saved = ["credential"]

        async def read(_):
            return saved[0]

        async def unset(*args):
            saved[0] = None
            return True

        monkeypatch.setattr(clients, "_client", lambda *a, **k: c)
        monkeypatch.setattr(users, "get_session" if kind == "user" else "get_helper_token", read)
        monkeypatch.setattr(users, "unset_field", unset)
        getter = clients.get_user_client if kind == "user" else clients.get_helper_bot
        remove = users.remove_session if kind == "user" else users.remove_helper_token
        create = asyncio.create_task(getter(1))
        await entered.wait()
        deletion = asyncio.create_task(remove(1))
        await asyncio.sleep(0)
        assert not deletion.done()
        proceed.set()
        assert await create is c
        assert await deletion
        assert await getter(1) is None
        assert c.stops == 1 and not c.is_connected

    asyncio.run(check())


@pytest.mark.parametrize("mode", ["connected", "initialized"])
@pytest.mark.parametrize("kind", ["user", "helper"])
def test_partial_start_failure_rolls_back(kind, mode, monkeypatch):
    c = Client(mode)
    monkeypatch.setattr(clients, "_client", lambda *a, **k: c)
    monkeypatch.setattr(
        users, "get_session" if kind == "user" else "get_helper_token", AsyncMock(return_value="x")
    )
    getter = clients.get_user_client if kind == "user" else clients.get_helper_bot
    assert asyncio.run(getter(1)) is None
    assert not c.is_connected and not c.is_initialized
    assert c.disconnects + c.stops == 1
    assert not clients._unclosed


def test_cancelled_start_rolls_back(monkeypatch):
    async def check():
        entered = asyncio.Event()
        c = Client(entered=entered, proceed=asyncio.Event())
        monkeypatch.setattr(clients, "_client", lambda *a, **k: c)
        monkeypatch.setattr(users, "get_session", AsyncMock(return_value="x"))
        job = asyncio.create_task(clients.get_user_client(1))
        await entered.wait()
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert not c.is_connected and c.disconnects == 1
        assert not clients._user_clients and not clients._unclosed

    asyncio.run(check())


def test_premium_failure_and_actual_state_shutdown(monkeypatch):
    b, p = Client(), Client("initialized")
    monkeypatch.setattr(clients, "bot", b)
    monkeypatch.setattr(clients, "premium", p)

    async def check():
        assert not await clients.start_main_clients()
        assert p.stops == 1 and not p.is_connected
        # 模拟标记与连接状态不一致：布尔标记 false，实际已连接。
        p.is_connected = True
        await clients.stop_all_clients()
        await clients.stop_all_clients()
        assert p.disconnects == 1 and b.stops == 1
        assert not clients._unclosed

    asyncio.run(check())


def test_shutdown_blocks_inflight_cache_insertion(monkeypatch):
    async def check():
        entered, proceed = asyncio.Event(), asyncio.Event()
        c = Client(entered=entered, proceed=proceed)
        monkeypatch.setattr(clients, "_client", lambda *a, **k: c)
        monkeypatch.setattr(clients, "bot", Client())
        monkeypatch.setattr(clients, "premium", None)
        monkeypatch.setattr(users, "get_session", AsyncMock(return_value="x"))
        job = asyncio.create_task(clients.get_user_client(1))
        await entered.wait()
        shutdown = asyncio.create_task(clients.stop_all_clients())
        await asyncio.sleep(0)
        proceed.set()
        assert await job is None
        await shutdown
        assert not c.is_connected and not clients._user_clients

    asyncio.run(check())


def test_ban_and_reallow_prevent_stale_credential_resurrection(monkeypatch):
    monkeypatch.setattr(users.storage, "delete_user", AsyncMock())
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(users, "set_field", save)

    async def check():
        assert await users.delete_user(42)
        assert not await users.save_session(42, "old session")
        save.assert_not_awaited()
        assert await users.set_whitelisted(42, True)
        assert await users.save_session(42, "new session")

    asyncio.run(check())


def test_successful_cleanup_is_idempotent_with_stale_closed_storage():
    async def check():
        c = Client()
        c.session = NS(stop=AsyncMock())
        c.storage = NS(conn=object(), close=AsyncMock())
        await clients._stop_quietly(c, "unused")
        await clients._stop_quietly(c, "unused")
        c.storage.close.assert_awaited_once()
        c.session.stop.assert_awaited_once()
        assert not clients._unclosed

    asyncio.run(check())


def test_partial_connect_failure_closes_resources_before_flag_set():
    async def check():
        c = Client()
        c.connect = AsyncMock(side_effect=RuntimeError("session start failed"))
        c.session = NS(stop=AsyncMock())
        c.storage = NS(conn=object(), close=AsyncMock())
        with pytest.raises(RuntimeError):
            await clients.safe_connect_client(c)
        c.session.stop.assert_awaited_once()
        c.storage.close.assert_awaited_once()
        assert not clients._unclosed

    asyncio.run(check())


def test_failure_to_stop_retains_reference_for_shutdown_retry(monkeypatch):
    async def check():
        c = Client()
        c.is_initialized = c.is_connected = True
        c.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        c.session = NS(stop=AsyncMock(side_effect=RuntimeError("session failed")))
        c.storage = NS(conn=object(), close=AsyncMock())
        await clients._stop_quietly(c, "failed")
        assert clients._unclosed[id(c)] is c
        c.session.stop.side_effect = None
        await clients._stop_quietly(c, "retry")
        assert not clients._unclosed and not c.is_connected

    asyncio.run(check())
