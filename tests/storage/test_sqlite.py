"""真实 SQLite 持久化、并发、取消、数据导入与崩溃恢复。"""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tgforward.storage import sqlite, users
from tgforward.storage.crypto import encrypt
from tgforward.storage.sqlite import initialize_empty
from tgforward.tools.import_mongo import MigrationError, migrate, read_ejson

ROOT = Path(__file__).resolve().parents[2]


def sample_user():
    return {
        "_id": "discard-root-id",
        "user_id": 42,
        "is_whitelisted": True,
        "session_string": encrypt("session"),
        "bot_token": encrypt("123:token"),
        "caption": "  **literal**  ",
        "chat_id": "-100123/5",
        "rename_tag": "测试",
        "delete_words": ["remove"],
        "replacement_words": {"old": "new"},
        "auto_comments": True,
        "stats": {"extracts": 8},
        "history": [{"m": 123, "t": datetime(2026, 9, 8, 1, 2, 3)}],
        "pending_logouts": {"key": {"session_string": encrypt("pending"), "error": "network"}},
        "future_field": {"nested": [1, {"$tf": ["datetime", "not-a-date"]}]},
    }


def test_codec_preserves_dates_and_unknown_fields():
    doc = sample_user()
    doc["aware"] = datetime(2026, 9, 8, tzinfo=UTC)
    assert sqlite.loads(sqlite.dumps(doc)) == doc


def test_store_pragmas_reopen_backup_and_full_document(sqlite_store, tmp_path):
    doc = sample_user()
    doc.pop("_id")
    backup = tmp_path / "backup.sqlite3"

    async def check():
        await sqlite_store.mutate_user(42, lambda target: target.update(doc))
        assert (
            await sqlite_store.run(
                lambda: sqlite_store.connection().execute("PRAGMA journal_mode").fetchone()[0]
            )
            == "wal"
        )
        assert (
            await sqlite_store.run(
                lambda: sqlite_store.connection().execute("PRAGMA synchronous").fetchone()[0]
            )
            == 2
        )
        assert (
            await sqlite_store.run(
                lambda: sqlite_store.connection().execute("PRAGMA busy_timeout").fetchone()[0]
            )
            == 5000
        )
        await sqlite_store.backup(backup)
        with pytest.raises(FileExistsError):
            await sqlite_store.backup(backup)
        await sqlite_store.close()
        await sqlite_store.initialize()
        assert await sqlite_store.get_user(42) == doc
        copied = sqlite.Store(backup)
        await copied.initialize()
        try:
            assert await copied.get_user(42) == doc
        finally:
            await copied.close()

    asyncio.run(check())
    assert Path(sqlite_store.path).stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o600


def test_two_connections_do_not_lose_writes(sqlite_store):
    async def check():
        other = sqlite.Store(sqlite_store.path)
        await other.initialize()
        try:

            def increment(doc):
                doc["count"] = doc.get("count", 0) + 1

            await asyncio.gather(
                *((sqlite_store if i % 2 else other).mutate_user(42, increment) for i in range(80))
            )
            assert (await sqlite_store.get_user(42))["count"] == 80
        finally:
            await other.close()

    asyncio.run(check())


def test_transaction_rolls_back_callback_and_encoding_errors(sqlite_store):
    async def check():
        await users.set_field(42, "caption", "original")
        before = await users.get_user(42)

        def fail(doc):
            doc["caption"] = "lost"
            raise RuntimeError("transaction interrupted")

        with pytest.raises(RuntimeError):
            await sqlite_store.mutate_user(42, fail)
        with pytest.raises(TypeError):
            await sqlite_store.mutate_user(42, lambda doc: doc.update(bad=object()))
        assert await users.get_user(42) == before

    asyncio.run(check())


def test_cancel_waits_for_worker_and_does_not_block_event_loop(sqlite_store):
    entered, proceed = threading.Event(), threading.Event()

    def slow(doc):
        entered.set()
        assert proceed.wait(5)
        doc["committed"] = True

    async def check():
        task = asyncio.create_task(sqlite_store.mutate_user(42, slow))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
        finally:
            proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await sqlite_store.get_user(42))["committed"]

    asyncio.run(check())


def test_user_apis_credentials_stats_history_pending_and_reset(sqlite_store, monkeypatch):
    from tgforward.runtime import lifecycle
    from tgforward.telegram import clients

    monkeypatch.setattr(clients, "remove_user_client", AsyncMock())
    monkeypatch.setattr(clients, "remove_helper_bot", AsyncMock())
    lifecycle.revoked.discard(42)

    async def check():
        assert await users.set_whitelisted(42, True)
        assert await users.save_session(42, "session")
        assert await users.save_helper_token(42, "123:token")
        assert await users.set_field(42, "caption", "extra")
        assert await users.set_field(42, "chat_id", "-100123/5")
        assert await users.update_word_rules(
            42, replacement=("old", "new"), delete_words=["remove"]
        )
        assert await users.toggle_auto_comments(42)
        assert await users.record_logout_pending(42, "pending", "network")
        await asyncio.gather(*(users.incr_stats(42, "extracts") for _ in range(30)))
        for i in range(25):
            await users.add_history(42, "channel", i, False, "summary")
        await sqlite_store.close()
        await sqlite_store.initialize()
        assert await users.get_session(42) == "session"
        assert await users.get_helper_token(42) == "123:token"
        assert await users.get_pending_logouts(42) == ["pending"]
        doc = await users.get_user(42)
        assert doc["stats"]["extracts"] == 30
        assert [item["m"] for item in doc["history"]] == list(range(5, 25))
        settings = await users.load_user_settings(42)
        assert settings.auto_comments and settings.replacements == {"old": "new"}
        assert await users.reset_settings(42)
        assert await users.get_session(42) == "session"
        assert (await users.get_user(42))["stats"] == doc["stats"]
        assert await users.clear_logout_pending(42, "pending")
        assert await users.get_pending_logouts(42) == []
        assert await users.remove_session(42) and await users.get_session(42) is None
        assert await users.remove_helper_token(42) and await users.get_helper_token(42) is None

    asyncio.run(check())


def test_whitelist_queries_and_ban_persist(sqlite_store, monkeypatch):
    from tgforward.telegram import clients

    monkeypatch.setattr(clients, "remove_user_client", AsyncMock())
    monkeypatch.setattr(clients, "remove_helper_bot", AsyncMock())

    async def check():
        for uid in [1, 42, 43, 44]:
            assert await users.set_whitelisted(uid, True)
        assert await users.set_whitelisted(45, False)
        assert await users.list_whitelisted_ids() == [1, 42, 43, 44]
        assert await users.count_whitelisted(exclude_owners=True) == 3
        assert await users.list_whitelisted_page(1, 1, exclude_owners=True) == [43]
        assert await users.is_whitelisted(42)
        assert await users.delete_user(42)
        assert not await users.is_whitelisted(42)
        assert await users.get_user(42) is None
        assert await users.storage_healthy()

    asyncio.run(check())


def test_schema_upgrade_removes_page_cache_but_preserves_users(sqlite_store):
    async def check():
        await users.save_session(42, "session")
        await users.set_field(42, "caption", "keep")

        def old_schema():
            conn = sqlite_store.connection()
            conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
            conn.execute("CREATE TABLE cache (key TEXT PRIMARY KEY, url TEXT, created_at REAL)")
            conn.execute("INSERT INTO cache VALUES ('post', 'unused', 1)")

        await sqlite_store.run(old_schema)
        await sqlite_store.close()
        await sqlite_store.initialize()
        assert await users.get_session(42) == "session"
        assert await users.get_field(42, "caption") == "keep"
        assert (
            await sqlite_store.run(
                lambda: (
                    sqlite_store.connection()
                    .execute("SELECT name FROM sqlite_master WHERE name='cache'")
                    .fetchone()
                )
            )
            is None
        )

    asyncio.run(check())


def test_migration_copies_every_field_and_ciphertext_atomically(sqlite_store):
    doc = sample_user()

    async def check():
        result = await migrate(sqlite_store, [doc])
        assert result["users"] == 1 and result["whitelisted"] == 1
        assert await sqlite_store.get_user(42) is None  # dry-run really rolls back
        await migrate(sqlite_store, [doc], apply=True)
        await sqlite_store.close()
        await sqlite_store.initialize()
        assert await sqlite_store.get_user(42) == {k: v for k, v in doc.items() if k != "_id"}
        assert await users.get_session(42) == "session"
        assert await users.get_helper_token(42) == "123:token"
        await users.set_field(42, "caption", "new SQLite edit")
        assert (await migrate(sqlite_store, [doc], apply=True))["already_complete"]
        assert await users.get_field(42, "caption") == "new SQLite edit"
        with pytest.raises(MigrationError):
            await migrate(sqlite_store, [{**doc, "caption": "different source"}], apply=True)

    asyncio.run(check())


def test_migration_failed_verification_rolls_back_data_and_marker(sqlite_store, monkeypatch):
    original = sqlite.Store.write_user

    def corrupt(conn, uid, doc):
        original(conn, uid, {**doc, "session_string": "corrupt"})

    monkeypatch.setattr(sqlite.Store, "write_user", staticmethod(corrupt))

    async def check():
        with pytest.raises(MigrationError):
            await migrate(sqlite_store, [sample_user()], apply=True)
        assert await sqlite_store.get_user(42) is None
        assert (
            await sqlite_store.run(
                lambda: (
                    sqlite_store.connection()
                    .execute("SELECT 1 FROM meta WHERE key='mongo_migration_complete'")
                    .fetchone()
                )
            )
            is None
        )

    asyncio.run(check())


@pytest.mark.parametrize("case", ["duplicate_user", "bad_id", "nonempty"])
def test_migration_rejects_ambiguous_or_used_targets(sqlite_store, case):
    doc = sample_user()
    source = [doc]
    if case == "duplicate_user":
        source.append(doc)
    if case == "bad_id":
        doc["user_id"] = True

    async def check():
        if case == "nonempty":
            await users.set_field(99, "caption", "keep")
        with pytest.raises(MigrationError):
            await migrate(sqlite_store, source, apply=True)
        assert await sqlite_store.get_user(42) is None
        if case == "nonempty":
            assert await users.get_field(99, "caption") == "keep"

    asyncio.run(check())


def test_missing_migration_marker_prevents_silent_empty_start(tmp_path, monkeypatch):
    from tgforward import config

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sqlite, "_store", None)

    async def check():
        with pytest.raises(RuntimeError, match="状态库尚未就绪"):
            await sqlite.initialize()
        assert sqlite._store is None
        store = sqlite.Store(tmp_path / "state" / "tgforward.sqlite3")
        await store.initialize()
        await initialize_empty(store)
        await store.close()
        await sqlite.initialize()
        assert await sqlite.healthcheck()
        await sqlite.close()

    asyncio.run(check())


def test_unknown_schema_not_overwritten(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '999')")
    store = sqlite.Store(path)
    with pytest.raises(ValueError):
        asyncio.run(store.initialize())
    assert store._connection is None
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM meta").fetchone()[0] == "999"


def test_ejson_import_without_mongo_driver_and_dry_run_creates_no_target(tmp_path):
    exported = tmp_path / "source.json"
    target = tmp_path / "state.sqlite3"
    bundle = {
        "users": [
            {
                "user_id": {"$numberLong": "42"},
                "is_whitelisted": True,
                "history": [{"t": {"$date": "2026-09-08T00:00:00Z"}}],
                "unknown": {"$oid": "1234567890abcdef12345678"},
            }
        ],
        "cache": [],
    }
    exported.write_text(json.dumps(bundle))
    args = [
        sys.executable,
        "-m",
        "tgforward.tools.import_mongo",
        "--from-json",
        str(exported),
        "--sqlite",
        str(target),
    ]
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRY-RUN" in result.stdout and not target.exists()
    result = subprocess.run(
        args + ["--apply"], cwd=ROOT, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 1 and not target.exists()
    result = subprocess.run(
        args + ["--apply", "--source-stopped"], cwd=ROOT, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.exists()
    assert read_ejson(bundle)["users"][0]["history"][0]["t"] == datetime(2026, 9, 8)


def test_runtime_imports_without_mongo_packages(tmp_path):
    code = """
import importlib.abc
import sys
class BlockMongo(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('motor', 'pymongo', 'bson'):
            raise AssertionError('runtime imported Mongo dependency: ' + fullname)
sys.meta_path.insert(0, BlockMongo())
from tgforward import config
from tgforward import app as main
from tgforward.telegram import clients
from tgforward import handlers
from tgforward.ui import keyboards
assert not config.validate_config(), config.validate_config()
from tgforward.storage import sqlite
assert sqlite._store is None
"""
    env = {**os.environ, "DATA_DIR": str(tmp_path), "MONGO_DB": "", "DB_NAME": ""}
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("commit", [False, True])
def test_process_crash_recovers_last_committed_state(sqlite_store, commit):
    asyncio.run(users.set_field(42, "caption", "before"))
    code = """
import os
import sqlite3
import sys
from tgforward.storage.sqlite import Store
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute('PRAGMA synchronous=FULL')
conn.execute('BEGIN IMMEDIATE')
doc = Store.read_user(conn, 42)
doc['caption'] = 'after'
Store.write_user(conn, 42, doc)
if sys.argv[2] == 'commit':
    conn.commit()
os._exit(17)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, sqlite_store.path, "commit" if commit else "rollback"],
        cwd=ROOT,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 17, result.stderr
    asyncio.run(sqlite_store.close())
    asyncio.run(sqlite_store.initialize())
    assert asyncio.run(users.get_field(42, "caption")) == ("after" if commit else "before")
