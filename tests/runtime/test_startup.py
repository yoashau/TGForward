"""启动事务清理及真实阻塞事件循环后的 watchdog 退出。"""

import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from tgforward import app as main
from tgforward.runtime import watchdog
from tgforward.storage import sqlite


@pytest.mark.parametrize("failure", ["database", "bot", "tgforward.handlers"])
def test_startup_failure_closes_acquired_resources(monkeypatch, failure, tmp_path):
    db = NS(initialize=AsyncMock(), close=AsyncMock())
    client = NS(start_main_clients=AsyncMock(), stop_all_clients=AsyncMock())
    resources = {
        name: NS(shutdown=AsyncMock())
        for name in [
            "tgforward.ui.interaction",
            "tgforward.ui.dialogue",
            "tgforward.runtime.tasks",
            "tgforward.handlers.router",
            "tgforward.transfers.delivery",
        ]
    }
    resources.update({"tgforward.storage.sqlite": db, "tgforward.telegram.clients": client})
    if failure == "database":
        db.initialize.side_effect = RuntimeError("database")
    elif failure == "bot":
        client.start_main_clients.side_effect = RuntimeError("bot")
    monkeypatch.setattr(main, "validate_config", lambda: [])
    monkeypatch.setattr(main, "import_module", resources.__getitem__)
    monkeypatch.setattr(
        main,
        "load_handlers",
        Mock(
            side_effect=RuntimeError("tgforward.handlers")
            if failure == "tgforward.handlers"
            else None
        ),
    )
    monkeypatch.setattr(main, "HEARTBEAT_FILE", str(tmp_path / ".heartbeat"))
    monkeypatch.setattr(main, "READY_FILE", str(tmp_path / ".ready"))
    wd = NS(start=Mock(), beat=Mock(), close=Mock())
    monkeypatch.setattr(watchdog, "Watchdog", lambda: wd)
    # 首个清理失败也不能跳过 Client 和 DB。
    resources["tgforward.runtime.tasks"].shutdown.side_effect = RuntimeError("cleanup")
    assert asyncio.run(main.main()) == 1
    db.close.assert_awaited_once()
    if failure != "database":
        client.stop_all_clients.assert_awaited_once()
        resources["tgforward.ui.interaction"].shutdown.assert_awaited_once()
    if failure == "bot":
        client.start_main_clients.assert_awaited_once()
        resources["tgforward.handlers.router"].shutdown.assert_awaited_once()
        resources["tgforward.transfers.delivery"].shutdown.assert_awaited_once()
    wd.close.assert_called_once()


def test_database_creation_failure_closes_candidate_and_close_is_idempotent(monkeypatch):
    fake = Mock()
    fake.execute = Mock(side_effect=ValueError("bad schema"))
    monkeypatch.setattr(sqlite.sqlite3, "connect", lambda *a, **k: fake)
    store = sqlite.Store(":memory:")
    with pytest.raises(ValueError):
        asyncio.run(store.initialize())
    fake.close.assert_called_once()
    asyncio.run(store.close())
    asyncio.run(store.close())
    fake.close.assert_called_once()


@pytest.mark.parametrize("stalled,expected", [(True, 70), (False, 0)])
def test_watchdog_exits_blocked_event_loop_but_not_healthy_loop(stalled, expected):
    code = f"""
import asyncio
import time
from tgforward.runtime.watchdog import Watchdog
async def run():
    watchdog=Watchdog(timeout=.2,interval=.02)
    watchdog.start()
    try:
        if {stalled!r}:
            time.sleep(2)
        else:
            for _ in range(20):
                watchdog.beat()
                await asyncio.sleep(.02)
    finally:
        watchdog.close()
asyncio.run(run())
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == expected, result.stderr
    assert ("heartbeat expired" in result.stderr) is stalled


def test_readiness_only_after_database_and_client_start(monkeypatch, tmp_path):
    ready = tmp_path / ".ready"
    ready.write_text("stale")
    monkeypatch.setattr(main, "READY_FILE", str(ready))
    monkeypatch.setattr(main, "HEARTBEAT_FILE", str(tmp_path / ".heartbeat"))
    monkeypatch.setattr(main, "validate_config", lambda: [])

    async def initialize():
        assert not ready.exists()
        await asyncio.sleep(0)
        assert not ready.exists()

    resources = {
        name: NS(shutdown=AsyncMock())
        for name in (
            "tgforward.ui.interaction",
            "tgforward.ui.dialogue",
            "tgforward.runtime.tasks",
            "tgforward.transfers.delivery",
            "tgforward.handlers.router",
        )
    }
    resources["tgforward.storage.sqlite"] = NS(initialize=initialize, close=AsyncMock())
    resources["tgforward.telegram.clients"] = NS(
        start_main_clients=initialize, stop_all_clients=AsyncMock()
    )
    resources["tgforward.handlers.start"] = NS(configure_menu=initialize)
    monkeypatch.setattr(main, "import_module", resources.__getitem__)
    monkeypatch.setattr(main, "load_handlers", Mock())
    monkeypatch.setattr(watchdog, "Watchdog", lambda: NS(start=Mock(), beat=Mock(), close=Mock()))
    real_event = asyncio.Event

    class StopEvent(real_event):
        async def wait(self):
            assert ready.read_text() == str(os.getpid())
            return True

    monkeypatch.setattr(main.asyncio, "Event", StopEvent)
    assert asyncio.run(main.main()) == 0
    assert not ready.exists()
