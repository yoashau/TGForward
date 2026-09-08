"""明确的 Telegram 限流重试与资源排队心跳；网络结果不明时不重发。"""

import asyncio
import time
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar

from pyrogram.errors import FloodWait

_current_task = ContextVar("telegram_task", default=None)
HEARTBEAT_INTERVAL = 5.0


@contextmanager
def task_scope(task):
    token = _current_task.set(task)
    try:
        yield
    finally:
        _current_task.reset(token)


def current_task():
    return _current_task.get()


def touch(task, stage):
    task = task or _current_task.get()
    if task:
        task.check_cancel()
        task.touch(stage)


async def heartbeat_sleep(
    seconds, task=None, stage="等待 Telegram 限流解除", *, status=None, notice=None
):
    deadline = time.monotonic() + max(0, seconds)
    next_notice = 0
    while True:
        touch(task, stage)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if status is not None and notice is not None and time.monotonic() >= next_notice:
            with suppress(Exception):
                await asyncio.wait_for(status.edit(notice(remaining)), timeout=5)
            next_notice = time.monotonic() + 30
        current = task or _current_task.get()
        if current is None:
            await asyncio.sleep(min(HEARTBEAT_INTERVAL, remaining))
            continue
        sleeper = asyncio.create_task(asyncio.sleep(min(HEARTBEAT_INTERVAL, remaining)))
        cancelled = asyncio.create_task(current.cancel_event.wait())
        try:
            await asyncio.wait((sleeper, cancelled), return_when=asyncio.FIRST_COMPLETED)
            current.check_cancel()
        finally:
            sleeper.cancel()
            cancelled.cancel()
            await asyncio.gather(sleeper, cancelled, return_exceptions=True)


@asynccontextmanager
async def acquire(resource, task=None, stage="等待传输资源"):
    task = task or _current_task.get()
    touch(task, stage)
    acquisition = asyncio.create_task(resource.acquire())
    cancelled = asyncio.create_task(task.cancel_event.wait()) if task is not None else None
    try:
        waits = [acquisition, cancelled] if cancelled is not None else [acquisition]
        while not acquisition.done():
            await asyncio.wait(
                waits, timeout=HEARTBEAT_INTERVAL, return_when=asyncio.FIRST_COMPLETED
            )
            touch(task, stage)
        await acquisition
        touch(task, stage)
        yield
    finally:
        if not acquisition.done():
            acquisition.cancel()
        if cancelled is not None:
            cancelled.cancel()
        await asyncio.gather(
            *([acquisition, cancelled] if cancelled else [acquisition]), return_exceptions=True
        )
        if not acquisition.cancelled() and acquisition.exception() is None and acquisition.result():
            resource.release()


async def retry_flood(make_call, task=None, stage="等待 Telegram 限流解除"):
    while True:
        touch(task, stage)
        try:
            return await make_call()
        except FloodWait as exc:
            await heartbeat_sleep(max(0, exc.value) + 1, task, stage)
