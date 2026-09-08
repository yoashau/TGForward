"""本地状态存储。事务在工作线程执行；导入不创建目录或连接。

同一连接由线程锁串行化，BEGIN IMMEDIATE 同时保护其他进程的写入。
取消调用仍等待已经提交到线程的事务结束，避免 close 与未完成的写入竞态。
"""

import asyncio
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = "2"
_store = None


def dumps(document):
    # 对原始字典中的保留标记作转义，未知旧字段也能无损往返。
    def encode(value):
        if isinstance(value, datetime):
            return {"$tf": ["datetime", value.isoformat()]}
        if isinstance(value, dict):
            result = {key: encode(item) for key, item in value.items()}
            return {"$tf": ["dict", result]} if "$tf" in value else result
        if isinstance(value, list):
            return [encode(item) for item in value]
        return value

    return json.dumps(
        encode(document), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def loads(document):
    def decode(value):
        if isinstance(value, dict):
            if set(value) == {"$tf"}:
                kind, payload = value["$tf"]
                if kind == "datetime":
                    return datetime.fromisoformat(payload)
                if kind == "dict":
                    return {key: decode(item) for key, item in payload.items()}
                raise ValueError("Unknown stored type")
            return {key: decode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [decode(item) for item in value]
        return value

    return decode(json.loads(document))


class Store:
    def __init__(self, path):
        self.path = str(path)
        self._connection = None
        self._lock = threading.RLock()

    async def run(self, operation):
        def locked():
            with self._lock:
                return operation()

        future = asyncio.create_task(asyncio.to_thread(locked))
        cancelled = False
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            # Retrieve any error; caller cancellation remains the outward result.
            if not future.cancelled():
                future.exception()
            raise asyncio.CancelledError
        return future.result()

    def connection(self):
        if self._connection is None:
            raise RuntimeError("SQLite 尚未初始化")
        return self._connection

    async def initialize(self):
        def open_database():
            if self._connection is not None:
                return
            if self.path != ":memory:":
                path = Path(self.path)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                os.close(fd)
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                version = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                if version is not None and version[0] not in ("1", SCHEMA_VERSION):
                    raise ValueError("不支持的 SQLite schema_version")
                conn.execute("""CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    is_whitelisted INTEGER NOT NULL DEFAULT 0 CHECK(is_whitelisted IN (0,1)),
                    document TEXT NOT NULL, updated_at INTEGER NOT NULL)""")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS users_whitelist ON users(is_whitelisted, user_id)"
                )
                # v1 的评论页面缓存已停用；账号和设置文档保持原样。
                conn.execute("DROP TABLE IF EXISTS cache")
                conn.execute(
                    "INSERT INTO meta VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (SCHEMA_VERSION,),
                )
                conn.commit()
            except BaseException:
                conn.close()
                raise
            self._connection = conn

        await self.run(open_database)

    async def close(self):
        def close_database():
            conn, self._connection = self._connection, None
            if conn is not None:
                conn.close()

        await self.run(close_database)

    def transaction(self, operation):
        conn = self.connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = operation(conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise

    @staticmethod
    def read_user(conn, user_id):
        row = conn.execute("SELECT document FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        return loads(row[0]) if row else None

    @staticmethod
    def write_user(conn, user_id, document):
        if document.get("user_id") != int(user_id):
            raise ValueError("user_id 与文档不一致")
        conn.execute(
            """INSERT INTO users VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET is_whitelisted=excluded.is_whitelisted,
            document=excluded.document, updated_at=excluded.updated_at""",
            (
                int(user_id),
                int(bool(document.get("is_whitelisted"))),
                dumps(document),
                int(time.time()),
            ),
        )

    async def get_user(self, user_id):
        return await self.run(lambda: self.read_user(self.connection(), user_id))

    async def mutate_user(self, user_id, mutate, *, create=True):
        def update(conn):
            document = self.read_user(conn, user_id)
            if document is None:
                if not create:
                    return None
                document = {"user_id": int(user_id)}
            result = mutate(document)
            self.write_user(conn, user_id, document)
            return result

        return await self.run(lambda: self.transaction(update))

    async def delete_user(self, user_id):
        await self.run(
            lambda: self.transaction(
                lambda conn: conn.execute(
                    "DELETE FROM users WHERE user_id=?",
                    (int(user_id),),
                )
            )
        )

    async def list_whitelisted_ids(self, offset=0, limit=None, exclude=()):
        def select():
            excluded = tuple(int(uid) for uid in exclude)
            where = "is_whitelisted=1"
            if excluded:
                where += " AND user_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
            rows = self.connection().execute(
                f"SELECT user_id FROM users WHERE {where} ORDER BY user_id LIMIT ? OFFSET ?",
                (*excluded, -1 if limit is None else max(0, limit), max(0, offset)),
            )
            return [row[0] for row in rows]

        return await self.run(select)

    async def count_whitelisted(self, exclude=()):
        def count():
            excluded = tuple(int(uid) for uid in exclude)
            where = "is_whitelisted=1"
            if excluded:
                where += " AND user_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
            return (
                self.connection()
                .execute(
                    f"SELECT COUNT(*) FROM users WHERE {where}",
                    excluded,
                )
                .fetchone()[0]
            )

        return await self.run(count)

    async def healthcheck(self):
        def check(conn):
            conn.execute("SELECT 1").fetchone()
            # A real write also checks file/directory permission and disk-full errors.
            conn.execute(
                "INSERT INTO meta VALUES ('healthcheck', ?) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value",
                (str(time.time()),),
            )
            return True

        return await self.run(lambda: self.transaction(check))

    async def backup(self, destination):
        def copy():
            # SQLite backup includes committed WAL pages; never copy only the live main file.
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            try:
                target = sqlite3.connect(destination)
                try:
                    self.connection().backup(target)
                finally:
                    target.close()
            except BaseException:
                Path(destination).unlink(missing_ok=True)
                raise

        await self.run(copy)


def current():
    if _store is None:
        raise RuntimeError("SQLite 尚未初始化")
    return _store


async def initialize(path=None):
    global _store
    if _store is not None:
        return
    from tgforward.config import DATA_DIR

    candidate = Store(path or Path(DATA_DIR) / "state" / "tgforward.sqlite3")
    try:
        await candidate.initialize()
        if path is None:
            ready = await candidate.run(
                lambda: (
                    candidate.connection()
                    .execute(
                        "SELECT 1 FROM meta WHERE key='state_initialized'",
                    )
                    .fetchone()
                )
            )
            if not ready:
                raise RuntimeError(
                    "状态库尚未就绪：旧部署先执行 Mongo 迁移；新部署执行 "
                    "python -m tgforward.tools.init_state"
                )
    except BaseException:
        await candidate.close()
        raise
    _store = candidate


async def close():
    global _store
    store, _store = _store, None
    if store is not None:
        await store.close()


async def get_user(user_id):
    return await current().get_user(user_id)


async def mutate_user(user_id, mutate, *, create=True):
    return await current().mutate_user(user_id, mutate, create=create)


async def delete_user(user_id):
    return await current().delete_user(user_id)


async def list_whitelisted_ids(offset=0, limit=None, exclude=()):
    return await current().list_whitelisted_ids(offset, limit, exclude)


async def count_whitelisted(exclude=()):
    return await current().count_whitelisted(exclude)


async def healthcheck():
    return await current().healthcheck()


async def initialize_empty(store):
    def initialize(conn):
        if conn.execute("SELECT 1 FROM meta WHERE key='state_initialized'").fetchone():
            return
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            raise ValueError("目标库已有数据，请执行迁移校验")
        conn.execute("INSERT INTO meta VALUES ('state_initialized', 'new')")

    await store.run(lambda: store.transaction(initialize))
