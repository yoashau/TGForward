"""停机迁移 Mongo 完整文档到 SQLite，默认仅在内存中验证。

--from-json 接受 mongosh EJSON.stringify({users:[...]}) 的导出文件；
该路径只用标准库。--mongo 从 MONGO_DB 读取，单独安装 requirements/import.txt。
迁移不导入加密模块，不解密、不生成密钥、不改动源数据。
"""

import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from tgforward.storage.sqlite import Store, dumps, loads


class MigrationError(ValueError):
    pass


def normalize(value):
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if type(value).__name__ == "ObjectId" and type(value).__module__.startswith("bson"):
        return str(value)
    return value


def read_ejson(value):
    if isinstance(value, list):
        return [read_ejson(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$oid"}:
        return value["$oid"]
    if set(value) in ({"$numberLong"}, {"$numberInt"}):
        return int(next(iter(value.values())))
    if set(value) == {"$numberDouble"}:
        return float(value["$numberDouble"])
    if set(value) == {"$date"}:
        date = value["$date"]
        if isinstance(date, str):
            return (
                datetime.fromisoformat(date.replace("Z", "+00:00"))
                .astimezone(UTC)
                .replace(tzinfo=None)
            )
        return datetime.fromtimestamp(read_ejson(date) / 1000, UTC).replace(tzinfo=None)
    return {key: read_ejson(item) for key, item in value.items()}


def prepare(users):
    documents = {}
    for original in users:
        doc = normalize({key: value for key, value in original.items() if key != "_id"})
        uid = doc.get("user_id")
        if isinstance(uid, bool) or not isinstance(uid, int) or not 0 < uid < 2**63:
            raise MigrationError("源用户 ID 缺失或非法")
        if uid in documents:
            raise MigrationError("源数据存在重复 user_id")
        # Verify the full serialization, not just a list of known fields.
        encoded = dumps(doc)
        if loads(encoded) != doc:
            raise MigrationError("用户文档序列化校验失败")
        documents[uid] = encoded
    signature = hashlib.sha256(
        dumps(
            {
                "users": {str(uid): value for uid, value in sorted(documents.items())},
            }
        ).encode()
    ).hexdigest()
    return documents, signature


async def migrate(store, users, *, apply=False):
    documents, signature = prepare(users)
    expected_whitelist = sum(bool(loads(doc).get("is_whitelisted")) for doc in documents.values())

    def import_data(conn):
        marker = conn.execute(
            "SELECT value FROM meta WHERE key='mongo_migration_complete'"
        ).fetchone()
        if marker:
            if marker[0] != signature:
                raise MigrationError("已迁移数据库与当前源不同，终止覆盖")
            return {
                "users": len(documents),
                "whitelisted": expected_whitelist,
                "already_complete": True,
            }
        if (
            conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            or conn.execute("SELECT 1 FROM meta WHERE key='state_initialized'").fetchone()
        ):
            raise MigrationError("目标库已经使用，终止覆盖")
        for uid, encoded in documents.items():
            Store.write_user(conn, uid, loads(encoded))
        actual = dict(conn.execute("SELECT user_id, document FROM users"))
        whitelist_count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_whitelisted=1"
        ).fetchone()[0]
        if actual != documents or whitelist_count != expected_whitelist:
            raise MigrationError("迁移后完整文档/白名单校验失败")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationError("SQLite 完整性校验失败")
        # Marker and data commit together; no separate file that can get ahead of COMMIT.
        conn.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [
                ("mongo_migration_complete", signature),
                ("state_initialized", "mongo"),
            ],
        )
        return {
            "users": len(documents),
            "whitelisted": expected_whitelist,
            "already_complete": False,
        }

    def transaction():
        conn = store.connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = import_data(conn)
            conn.commit() if apply else conn.rollback()
            return result
        except BaseException:
            conn.rollback()
            raise

    return await store.run(transaction)


def read_source(args):
    if args.from_json:
        bundle = json.loads(Path(args.from_json).read_text())
        if not isinstance(bundle, dict) or not isinstance(bundle.get("users"), list):
            raise MigrationError("导出文件必须包含 users 数组")
        return read_ejson(bundle["users"])
    from pymongo import MongoClient

    uri = os.getenv("MONGO_DB", "").strip()
    if not uri:
        raise MigrationError("请设置迁移源 MONGO_DB")
    with MongoClient(uri, serverSelectionTimeoutMS=10000) as client:
        db = client[args.database]
        # The operator must keep every old writer stopped for the whole export/import.
        users = list(db.users.find({}))
        if len(users) != db.users.count_documents({}):
            raise MigrationError("读取期间源数量发生变化，请停止旧写入端")
        return users


async def main(args):
    if args.apply and not args.source_stopped:
        raise MigrationError("写入迁移前需停机并传入 --source-stopped")
    source = await asyncio.to_thread(read_source, args)
    if not source and not args.allow_empty_source:
        raise MigrationError("源用户数为零；请核对源数据库，确为空才使用 --allow-empty-source")
    store = Store(args.sqlite if args.apply else ":memory:")
    try:
        await store.initialize()
        counts = await migrate(store, source, apply=args.apply)
        print("APPLY" if args.apply else "DRY-RUN", counts)
    finally:
        await store.close()


if __name__ == "__main__":
    # Only the CLI reads deployment configuration. Importing the migration library
    # neither validates production credentials nor performs decryption.
    from tgforward.config import DATA_DIR

    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-json", help="停机导出的 EJSON bundle 文件")
    source.add_argument("--mongo", action="store_true", help="读取 MONGO_DB（仅迁移工具使用）")
    parser.add_argument("--sqlite", default=str(Path(DATA_DIR) / "state" / "tgforward.sqlite3"))
    parser.add_argument("--database", default=os.getenv("DB_NAME", "telegram_downloader"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--source-stopped", action="store_true")
    parser.add_argument("--allow-empty-source", action="store_true")
    try:
        asyncio.run(main(parser.parse_args()))
    except Exception as exc:
        # Never echo the URI, document contents, ciphertext or keys.
        print(
            f"MIGRATION ERROR: {exc}"
            if isinstance(exc, MigrationError)
            else f"MIGRATION ERROR: {type(exc).__name__}"
        )
        raise SystemExit(1) from None
