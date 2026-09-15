"""提取任务注册表：单条与批量统一管理，支持协作式取消。

任务只在内存中登记（任务本身无法跨重启恢复，持久化只会带来
"重启后用户被锁死"的问题），重启即清空。
"""

import asyncio
import logging
import time
from collections import OrderedDict
from contextlib import contextmanager, suppress
from contextvars import copy_context
from dataclasses import dataclass, field
from enum import IntEnum
from uuid import uuid4

from tgforward.config import TASK_STALL_TIMEOUT, USER_COOLDOWN
from tgforward.runtime import lifecycle
from tgforward.transfers.results import CommentResult, ExtractionUnit
from tgforward.ui.i18n import tr

UPLOAD_CANCEL_TIMEOUT = 15


class TaskAlreadyActive(Exception):
    """该用户已有进行中的任务。"""


class QueueFull(Exception):
    """该用户的等待队列已满。"""


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
    queued_message: object | None = field(default=None, repr=False)
    uploading: bool = False
    upload_interrupted: bool = False
    cancel_confirmation: str | None = None
    cancel_confirmation_deadline: float = 0
    _confirmation_runner: asyncio.Task | None = field(default=None, repr=False)

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
        text += tr("\n📋 等待队列：{0} 个请求", queued_count(self.user_id))
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
        self.dismiss_upload_cancel()
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

    @contextmanager
    def upload_scope(self):
        self.uploading = True
        self.upload_interrupted = False
        try:
            yield
        finally:
            self.uploading = False
            pending = self.cancel_confirmation is not None or self._confirmation_runner is not None
            self.dismiss_upload_cancel()
            if pending and not self.cancelled and self.status is not None:
                self._confirmation_runner = asyncio.create_task(
                    self._restore_upload_progress(self.status)
                )

    @property
    def awaiting_upload_cancel(self):
        return (
            self.uploading
            and not self.cancelled
            and self.cancel_confirmation is not None
            and time.monotonic() < self.cancel_confirmation_deadline
        )

    def dismiss_upload_cancel(self):
        self.cancel_confirmation = None
        self.cancel_confirmation_deadline = 0
        if self._confirmation_runner is not None:
            self._confirmation_runner.cancel()
            self._confirmation_runner = None

    async def _restore_upload_progress(self, status):
        try:
            if status is not None and self.status is status:
                with suppress(Exception):
                    await asyncio.wait_for(status.refresh_controls(), timeout=5)
        finally:
            if self._confirmation_runner is asyncio.current_task():
                self._confirmation_runner = None

    def confirm_upload_cancel(self):
        if not self.uploading or self.cancelled:
            return False
        if not self.awaiting_upload_cancel:
            self.dismiss_upload_cancel()
            self.cancel_confirmation = uuid4().hex[:12]
            self.cancel_confirmation_deadline = time.monotonic() + UPLOAD_CANCEL_TIMEOUT
            confirmation = self.cancel_confirmation
            deadline = self.cancel_confirmation_deadline

            async def expire():
                await asyncio.sleep(max(0, deadline - time.monotonic()))
                if self.cancel_confirmation != confirmation:
                    return
                self.cancel_confirmation = None
                self.cancel_confirmation_deadline = 0
                await self._restore_upload_progress(self.status)

            self._confirmation_runner = asyncio.create_task(expire())
        return True

    def advance(self, *, success: bool = False) -> None:
        self.touch()
        self.current += 1
        if self.active_unit is not None:
            self.active_unit.advance()
        if success:
            self.success += 1


_tasks: dict[int, Task] = {}
_last_finished: dict[int, float] = {}
_shutting_down = False
QUEUE_LIMIT = 9999


@dataclass
class QueuedRequest:
    user_id: int
    kind: str
    total: int
    work: object
    notify: object
    permit: lifecycle.Permit
    context: object = field(default_factory=copy_context, repr=False)
    token: str = field(default_factory=lambda: uuid4().hex[:12])
    messages: object | None = field(default=None, repr=False)
    notice: object | None = field(default=None, repr=False)
    cancelled: bool = False


_queues: dict[int, OrderedDict[str, QueuedRequest]] = {}


def queued_count(user_id):
    return len(_queues.get(user_id, ()))


def is_queued(user_id, token):
    return token in _queues.get(user_id, {})


def submit(user_id, kind, total, work, notify):
    """只为队首创建 Task/runner；等待请求按 FIFO 保存，增删均为 O(1)。"""
    from tgforward.ui import interaction as ui

    if _shutting_down:
        raise lifecycle.StalePermit("task service is shutting down")
    permit = lifecycle.capture_permit(user_id)
    if queued_count(user_id) >= QUEUE_LIMIT:
        raise QueueFull()
    request = QueuedRequest(user_id, kind, total, work, notify, permit, messages=ui.defer())
    if is_active(user_id):
        _queues.setdefault(user_id, OrderedDict())[request.token] = request
        return request, queued_count(user_id)
    _start_request(request)
    return request, 0


def _start_request(request):
    lifecycle.assert_current(request.permit)
    uid = request.user_id
    delay = max(0, _last_finished.get(uid, 0) + USER_COOLDOWN - time.monotonic())
    task = Task(
        uid, request.kind, request.total, token=request.token, lifecycle_permit=request.permit
    )
    _tasks[uid] = task

    async def work():
        # 路由持锁发送排队提示；等提示就绪后交给首个链接作为进度消息。
        async with lifecycle.user_lock(uid):
            task.queued_message, request.notice = request.notice, None
        await task.wait_or_cancel(delay, tr("等待任务间隔"))
        await request.work(task)

    async def notify(text):
        async with lifecycle.user_lock(uid):
            message = request.notice or task.queued_message
            if message is not None:
                from tgforward.transfers.progress import stop_keyboard

                await message.edit(
                    text, reply_markup=None if task.cancelled else stop_keyboard(task)
                )
                return
        await request.notify(text)

    request.context.run(launch, task, work, notify)


def _release_requests(requests):
    from tgforward.ui.interaction import Messages

    messages = Messages()
    for request in requests:
        request.cancelled = True
        if request.messages is not None:
            for message in list(request.messages.items.values()):
                messages.add(message)
    if messages.items:
        messages.later()


def remove_queued(user_id, token):
    queue = _queues.get(user_id)
    request = queue.pop(token, None) if queue else None
    if request is None:
        return False
    if not queue:
        _queues.pop(user_id, None)
    _release_requests([request])
    return True


def clear_queue(user_id):
    queue = _queues.pop(user_id, {})
    _release_requests(queue.values())
    return len(queue)


def register(user_id: int, kind: str, total: int) -> Task:
    if _shutting_down:
        raise lifecycle.StalePermit("task service is shutting down")
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
    if finished is not None:
        finished.dismiss_upload_cancel()
    if finished and (finished.cancelled or finished.timed_out):
        _last_finished.pop(user_id, None)
    else:
        _last_finished[user_id] = time.monotonic()
    if finished is not None:
        queue = _queues.get(user_id)
        while queue:
            _, request = queue.popitem(last=False)
            if not queue:
                _queues.pop(user_id, None)
            try:
                _start_request(request)
            except lifecycle.StalePermit:
                _release_requests([request])
                continue
            break


def is_active(user_id: int) -> bool:
    return user_id in _tasks


def all_active() -> dict[int, Task]:
    """全部进行中任务（供 /status 展示）。"""
    return dict(_tasks)


def request_cancel(user_id: int, reason=CancelReason.USER) -> bool:
    if reason in (CancelReason.REVOKED, CancelReason.SHUTDOWN):
        clear_queue(user_id)
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
        status = task.status or (task.units[-1].status if task.units else None)
        if status is None and task.queued_message is not None:
            from tgforward.transfers.progress import TaskStatus

            status = task.status = TaskStatus.wrap(task.queued_message, task)
            task.queued_message = None
        if status is not None:
            if status.terminal_rendered:
                return
            with suppress(Exception):
                await asyncio.wait_for(
                    status.finish(
                        text, "stopped" if task.cancelled or task.timed_out else "failed"
                    ),
                    timeout=5,
                )
            if status.terminal_rendered:
                return
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
                text = tr("⏳ {0}在「{1}」阶段暂时没有新进度。", kind, tr(task.stage))
                if task.status is not None:
                    with suppress(Exception):
                        await asyncio.wait_for(task.status.edit(text), timeout=5)
                else:
                    await say(text)
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
    global _shutting_down
    _shutting_down = True
    for uid in list(_queues):
        clear_queue(uid)
    runners = [t.runner for t in _tasks.values() if t.runner and not t.runner.done()]
    for task in list(_tasks.values()):
        request_cancel(task.user_id, CancelReason.SHUTDOWN)
        if task.runner is None:
            finish(task.user_id, task)
    if runners:
        await asyncio.gather(*runners, return_exceptions=True)
