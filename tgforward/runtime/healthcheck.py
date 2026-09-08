"""启动就绪与事件循环活性分开检查；unhealthy 本身不触发重启。"""

import os
import time
from pathlib import Path


def healthy(root=".", now=None):
    root = Path(root)
    try:
        pid = int((root / ".ready").read_text())
        if pid <= 0:
            return False
        os.kill(pid, 0)
        age = (time.time() if now is None else now) - (root / ".heartbeat").stat().st_mtime
        return 0 <= age < 90
    except (OSError, ValueError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if healthy() else 1)
