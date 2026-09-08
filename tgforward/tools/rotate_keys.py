"""离线停机密钥迁移：默认 dry-run，--apply 后事务更新 SQLite，可重复执行。"""

import argparse
import asyncio
import os
import re

from cryptography.exceptions import InvalidTag

from tgforward.config import _PUBLIC_MASTER_KEY, _PUBLIC_SALT
from tgforward.storage.crypto import _derive_key, decrypt, encrypt
from tgforward.storage.sqlite import Store, loads
from tgforward.storage.users import _set_path


def credential_fields(doc):
    for field in ("session_string", "bot_token"):
        if doc.get(field):
            yield field, doc[field]
    for identity, pending in (doc.get("pending_logouts") or {}).items():
        if pending.get("session_string"):
            yield f"pending_logouts.{identity}.session_string", pending["session_string"]


def plan_document(doc, old_key, new_key):
    """先认证全部字段再生成更新；出错不写该文档，不打印凭证或密钥。"""
    old, changed = {}, {}
    for field, stored in credential_fields(doc):
        if stored.startswith("v1:"):
            try:
                decrypt(stored, key=new_key)
                continue  # 已迁移字段；允许中断后重跑。
            except InvalidTag:
                pass
        if field == "bot_token" and re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", stored):
            plain = stored
        else:
            plain = decrypt(stored, key=old_key)
        old[field] = stored
        changed[field] = encrypt(plain, key=new_key)
    return old, changed


async def migrate(store, old_key, new_key, *, apply=False):
    def change(conn):
        counts = {"documents": 0, "fields": 0, "updated": 0, "conflicts": 0, "errors": 0}
        plans = []
        for uid, encoded in conn.execute("SELECT user_id, document FROM users"):
            doc = loads(encoded)
            try:
                _, changed = plan_document(doc, old_key, new_key)
            except (ValueError, TypeError, AttributeError, InvalidTag):
                counts["errors"] += 1
                continue
            if changed:
                counts["documents"] += 1
                counts["fields"] += len(changed)
                plans.append((uid, doc, changed))
        if apply and not counts["errors"]:
            for uid, doc, changed in plans:
                for field, value in changed.items():
                    _set_path(doc, field, value)
                Store.write_user(conn, uid, doc)
                counts["updated"] += 1
        return counts

    return await store.run(lambda: store.transaction(change))


async def _main(apply):
    from tgforward.storage import sqlite as storage

    values = [
        os.getenv(n, "").strip()
        for n in ("OLD_MASTER_KEY", "OLD_SALT_KEY", "NEW_MASTER_KEY", "NEW_SALT_KEY")
    ]
    if not all(values):
        raise ValueError("必须配置 OLD/NEW_MASTER_KEY、OLD/NEW_SALT_KEY")
    if len(values[2]) < 32 or values[2] == _PUBLIC_MASTER_KEY or values[3] == _PUBLIC_SALT:
        raise ValueError("新 MASTER_KEY 至少 32 字符且新密钥/盐不得使用旧公开默认值")
    old_key, new_key = _derive_key(*values[:2]), _derive_key(*values[2:])
    try:
        await storage.initialize()
        counts = await migrate(storage.current(), old_key, new_key, apply=apply)
        print("APPLY" if apply else "DRY-RUN", counts)
        return 1 if counts["errors"] or counts["conflicts"] else 0
    finally:
        await storage.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="写入已认证的新密文；默认仅验证与统计")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_main(args.apply)))
    except Exception as exc:
        # 不输出环境变量或异常中可能包含的密文。
        print(f"MIGRATION ERROR: {type(exc).__name__}")
        raise SystemExit(1) from None
