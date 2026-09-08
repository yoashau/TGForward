"""初始化全新实例的状态库，已有数据保持不变。"""

import asyncio
from pathlib import Path

from tgforward.config import DATA_DIR, validate_config
from tgforward.storage.sqlite import Store, initialize_empty


async def initialize():
    errors = validate_config()
    if errors:
        raise ValueError("；".join(errors))
    store = Store(Path(DATA_DIR) / "state" / "tgforward.sqlite3")
    try:
        await store.initialize()
        await initialize_empty(store)
    finally:
        await store.close()


if __name__ == "__main__":
    try:
        asyncio.run(initialize())
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
    print("状态库已就绪。")
