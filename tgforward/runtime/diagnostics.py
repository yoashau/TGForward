"""运行时诊断：最近错误的内存环形缓冲（供 /status 展示）。

最近 50 条记录保留在内存中，供管理员查询。
"""

import time
from collections import deque
from datetime import datetime, timedelta, timezone

_MAX_ERRORS = 50
_TZ = timezone(timedelta(hours=8))

_errors: deque = deque(maxlen=_MAX_ERRORS)


def record_error(source: str, message: str) -> None:
    """记录一条错误（source 为模块/操作名，message 自动截断）。"""
    _errors.append({"t": time.time(), "source": source, "msg": str(message)[:200]})


def recent_errors(limit: int = 5) -> list:
    """最近错误，最新的在前。"""
    return list(_errors)[-limit:][::-1]


def format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, _TZ).strftime("%m-%d %H:%M:%S")
