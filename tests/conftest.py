"""测试环境占位凭据：仅用于构造客户端对象，不建立任何连接。"""

import os

os.environ.setdefault("API_ID", "12345")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "123456:ABCdef1234567890abcdef")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("MASTER_KEY", "test-only-independent-master-key-2026-0123456789")
os.environ.setdefault("SALT_KEY", "test-only-independent-salt-2026")


import pytest


@pytest.fixture(autouse=True)
def isolate_menu(monkeypatch):
    from tgforward.ui import panel

    panel._panels.clear()
    panel._keyboards_removed.clear()
    yield
    panel._panels.clear()
    panel._keyboards_removed.clear()


@pytest.fixture
def sqlite_store(tmp_path, monkeypatch):
    import asyncio

    from tgforward.runtime import lifecycle
    from tgforward.storage import sqlite, users

    revoked = set(lifecycle.revoked)
    store = sqlite.Store(tmp_path / "state" / "tgforward.sqlite3")
    asyncio.run(store.initialize())
    monkeypatch.setattr(sqlite, "_store", store)
    users._whitelist_cache.clear()
    yield store
    asyncio.run(store.close())
    users._whitelist_cache.clear()
    lifecycle.revoked.clear()
    lifecycle.revoked.update(revoked)
