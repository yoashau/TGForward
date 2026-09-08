"""提取任务注册表：单条与批量统一管理，支持协作式取消。

任务只在内存中登记（任务本身无法跨重启恢复，持久化只会带来
"重启后用户被锁死"的问题），重启即清空。
"""

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from enum import IntEnum
from uuid import uuid4

from tgforward.config import TASK_STALL_TIMEOUT, USER_COOLDOWN
from tgforward.runtime import lifecycle
from tgforward.transfers.results import CommentResult, ExtractionUnit
from tgforward.ui.i18n import tr


class TaskAlreadyActive(Exception):
    """该用户已有进行中的任务。"""


class TaskCooldown(Exception):
    """距上次任务结束太近，处于操作冷却期。

    remaining 为需等待的秒数。
    """

    def __init__(self, remaining: float):
        self.remaining = remaining
        super().__init__(f"cooldown {remaining:.1f}s")


class TaskCancelled(Exception):
    """在传输回调中抛出以中止当前的下载/上传。"""


class CancelReason(IntEnum):
    USER = 1
    TIMEOUT = 2
    SHUTDOWN = 3
    REVOKED = 4


@dataclass
class Task:
    user_id: int
    kind: str  # "single" / "batch" / "comments"
    total: int
    current: int = 0
    success: int = 0
    cancel_requested: bool = False
    cancel_reason: CancelReason | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    token: str = field(default_factory=lambda: uuid4().hex[:12])
    runner: asyncio.Task | None = field(default=None, repr=False)
    last_activity: float = field(default_factory=time.monotonic)
    stage: str = "获取消息"
    timed_out: bool = False
    status: object | None = field(default=None, repr=False)

    lifecycle_permit: lifecycle.Permit | None = field(default=None, repr=False)

    units: list[ExtractionUnit] = field(default_factory=list, repr=False)
    active_unit: ExtractionUnit | None = field(default=None, repr=False)
    _units_by_key: dict = field(default_factory=dict, repr=False)

    def extraction_unit(self, input_key, request_total=1):
        """输入位置是身份的一部分；同一位置重入保留 Unit 与 MessageResult ID。"""
        if input_key not in self._units_by_key:
            unit = ExtractionUnit(f"unit:{len(self.units)}", self.token, request_total)
            self._units_by_key[input_key] = unit
            self.units.append(unit)
        unit = self._units_by_key[input_key]
        if unit.request_total != request_total:
            raise ValueError("an extraction unit cannot change its requested range")
        return unit

    media_known: set = field(default_factory=set, repr=False)
    media_downloads: set = field(default_factory=set, repr=False)
    media_downloaded: set = field(default_factory=set, repr=False)
    media_sent: set = field(default_factory=set, repr=False)
    media_failed: set = field(default_factory=set, repr=False)
    media_scope: str = "当前链接"
    media_scanning: bool = False
    comment_result: CommentResult | None = None

    @property
    def comments_incomplete(self):
        return self.comment_result is not None and self.comment_result.incomplete

    comment_target: tuple | None = None

    @staticmethod
    def is_media(message):
        return any(
            getattr(message, kind, None)
            for kind in (
                "photo",
                "video",
                "audio",
                "document",
                "animation",
                "voice",
                "video_note",
                "sticker",
            )
        )

    @staticmethod
    def media_key(message):
        return (message.chat.id, message.id)

    def discover(self, message, private):
        if self.is_media(message):
            key = self.media_key(message)
            self.media_known.add(key)
            if private:
                self.media_downloads.add(key)

    def media_progress(self):
        known_sent, known_failed, unknown = set(), set(), set()
        owners = [self.active_unit] if self.active_unit is not None else self.units
        modeled = False
        for unit in owners:
            for source, result in unit._messages.items():
                if not result.delivery.sealed:
                    continue
                modeled = True
                chat_id = source[0]
                for part_id, part in result.delivery.parts.items():
                    if not part_id.startswith("media:"):
                        continue
                    key = (chat_id, part.source_message_id)
                    if key not in self.media_known:
                        continue
                    if part_id in result.delivery.confirmed_delivered:
                        known_sent.add(key)
                    elif part_id in result.delivery.confirmed_failed:
                        known_failed.add(key)
                    elif part_id in result.delivery.uncertain:
                        unknown.add(key)
        sent = len(known_sent) if modeled else len(self.media_sent)
        failed = len(known_failed) if modeled else len(self.media_failed)
        total = len(self.media_known)
        downloads, downloaded = len(self.media_downloads), len(self.media_downloaded)
        pending = len(self.media_downloads - self.media_downloaded - self.media_failed)
        text = tr(
            "tasks.media_progress",
            tr(self.media_scope),
            total,
            sent,
            failed,
            downloads,
            downloaded,
            pending,
        )
        if unknown:
            text += tr("\n⚠️ {0} 个媒体发送结果无法确认。", len(unknown))
        if self.media_scanning:
            text += tr("\n正在读取清单，数量可能增加。")
        return text

    def reset_media(self, scope):
        self.media_scope = scope
        self.comment_result = None
        for items in (
            self.media_known,
            self.media_downloads,
            self.media_downloaded,
            self.media_sent,
            self.media_failed,
        ):
            items.clear()

    def touch(self, stage: str | None = None) -> None:
        self.last_activity = time.monotonic()
        if stage:
            self.stage = stage

    @property
    def cancelled(self) -> bool:
        return self.cancel_requested

    def set_cancel_reason(self, reason):
        reason = CancelReason(reason)
        if self.cancel_reason is None or reason > self.cancel_reason:
            self.cancel_reason = reason
        self.cancel_requested = True
        self.cancel_event.set()
        if self.cancel_reason == CancelReason.TIMEOUT:
            self.timed_out = True

    async def wait_or_cancel(self, seconds, stage=None):
        self.check_cancel()
        self.touch(stage)
        with suppress(TimeoutError):
            await asyncio.wait_for(self.cancel_event.wait(), timeout=max(0, seconds))
        self.check_cancel()

    def check_cancel(self) -> None:
        if self.cancel_requested:
            raise TaskCancelled()

    def advance(self, *, success: bool = False) -> None:
        self.touch()
        self.current += 1
        if self.active_unit is not None:
            self.active_unit.advance()
        if success:
            self.success += 1


_tasks: dict[int, Task] = {}
_last_finished: dict[int, float] = {}


def register(user_id: int, kind: str, total: int) -> Task:
    last = _last_finished.get(user_id)
    if last is not None:
        elapsed = time.monotonic() - last
        if elapsed < USER_COOLDOWN:
            raise TaskCooldown(USER_COOLDOWN - elapsed)
    if user_id in _tasks:
        raise TaskAlreadyActive()
    permit = lifecycle.capture_permit(user_id)
    task = Task(user_id=user_id, kind=kind, total=total, lifecycle_permit=permit)
    _tasks[user_id] = task
    return task


def get(user_id: int) -> Task | None:
    return _tasks.get(user_id)


def finish(user_id: int, expected: Task | None = None) -> None:
    if expected is not None and _tasks.get(user_id) is not expected:
        return
    finished = _tasks.pop(user_id, None)
    if finished and (finished.cancelled or finished.timed_out):
        _last_finished.pop(user_id, None)
    else:
        _last_finished[user_id] = time.monotonic()


def is_active(user_id: int) -> bool:
    return user_id in _tasks


def all_active() -> dict[int, Task]:
    """全部进行中任务（供 /status 展示）。"""
    return dict(_tasks)


def request_cancel(user_id: int, reason=CancelReason.USER) -> bool:
    task = _tasks.get(user_id)
    if task is None:
        return False
    task.set_cancel_reason(reason)
    if (
        task.cancel_reason != CancelReason.USER
        and task.runner is not None
        and not task.runner.done()
    ):
        task.runner.cancel()
    return True


logger = logging.getLogger(__name__)


def launch(task: Task, work, notify) -> None:
    """提取在独立协程运行，不占用 Dispatcher worker；取消/卡住都释放登记。"""

    from tgforward.ui import interaction as ui

    if task.runner is not None or _tasks.get(task.user_id) is not task:
        return
    if task.cancel_requested:
        finish(task.user_id, task)
        return

    messages = ui.defer()

    async def say(text):
        if task.cancel_reason in (CancelReason.REVOKED, CancelReason.SHUTDOWN):
            return

        async def notify_current():
            if task.cancel_reason in (CancelReason.REVOKED, CancelReason.SHUTDOWN):
                return
            lifecycle.assert_current(task.lifecycle_permit)
            await notify(text)

        with suppress(Exception):
            await asyncio.wait_for(notify_current(), timeout=10)

    async def terminal(text):
        if task.cancel_reason == CancelReason.REVOKED:
            return
        if task.status is not None:
            with suppress(Exception):
                await asyncio.wait_for(
                    task.status.finish(
                        text, "stopped" if task.cancelled or task.timed_out else "failed"
                    ),
                    timeout=5,
                )
        await say(text)

    async def watch():
        warned = False
        while True:
            await asyncio.sleep(min(30, TASK_STALL_TIMEOUT / 2))
            idle = time.monotonic() - task.last_activity
            if idle >= TASK_STALL_TIMEOUT:
                request_cancel(task.user_id, CancelReason.TIMEOUT)
                return
            if idle >= min(60, TASK_STALL_TIMEOUT / 2) and not warned:
                warned = True
                kind = tr("评论提取") if task.kind == "comments" else tr("消息提取")
                await say(
                    tr(
                        "⏳ {0}在「{1}」阶段暂时没有新进度，可发送 /cancel 停止。",
                        kind,
                        tr(task.stage),
                    )
                )
            if idle < 30:
                warned = False

    async def run():
        logger.info("task=%s user=%s started total=%s", task.token, task.user_id, task.total)
        watcher = asyncio.create_task(watch())
        try:
            task.check_cancel()
            from tgforward.telegram.wait import task_scope

            with task_scope(task):
                await work()
            logger.info(
                "task=%s finished current=%s success=%s", task.token, task.current, task.success
            )
        except (asyncio.CancelledError, TaskCancelled):
            task.cancel_requested = True
            logger.info(
                "task=%s user=%s cancelled timeout=%s stage=%s",
                task.token,
                task.user_id,
                task.timed_out,
                task.stage,
            )
            await terminal(
                tr("⚠️ 任务长时间没有进度，已停止并释放；请重新发送链接。")
                if task.timed_out
                else tr("💬 评论提取已停止，可点击按钮重试。")
                if task.kind == "comments"
                else tr("🚫 提取任务已停止，可以重新发送链接。")
            )
        except Exception:
            logger.exception(
                "task=%s user=%s stage=%s failed", task.token, task.user_id, task.stage
            )
            await terminal(tr("⚠️ 任务执行出错，已释放；请重试或提供操作时间用于排查。"))
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            finish(task.user_id, task)

    task.runner = asyncio.create_task(run(), name=f"extract:{task.token}")

    # 任务在第一次运行前就被取消时，协程 finally 尚未执行，也必须释放。
    def done(_):
        finish(task.user_id, task)
        if messages:
            messages.later()

    task.runner.add_done_callback(done)


async def shutdown() -> None:
    runners = [t.runner for t in _tasks.values() if t.runner and not t.runner.done()]
    for task in list(_tasks.values()):
        request_cancel(task.user_id, CancelReason.SHUTDOWN)
    if runners:
        await asyncio.gather(*runners, return_exceptions=True)
