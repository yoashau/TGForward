"""上传/下载进度条：按文件大小分档节流，编辑 Telegram 状态消息。

进度回调同时是取消检查点：任务请求取消时抛出库支持的 StopTransmission，
Pyrogram 会中止进行中的传输。
"""

import asyncio
import contextlib
import time

from pyrogram import StopTransmission
from pyrogram.errors import MessageNotModified

from tgforward.runtime.tasks import CancelReason, Task
from tgforward.transfers.results import SideEffectRole


def stop_keyboard(task):
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏹ 停止提取评论" if task.kind == "comments" else "⏹ 停止提取消息",
                    callback_data=f"flow:cancel:{task.user_id}:{task.token}",
                )
            ]
        ]
    )


def result_keyboard(task, outcome, comment_action=None):
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    label = {
        "success": "✅ 提取消息成功",
        "partial": "⚠️ 提取结束，部分未完成",
        "incomplete": "⚠️ 消息提取未完成",
        "uncertain": "⚠️ 消息发送结果无法确认",
        "failed": "⚠️ 提取失败",
        "stopped": "⏹ 提取已停止",
    }[outcome]
    from tgforward.ui.keyboards import comment_button

    row = [InlineKeyboardButton(label, callback_data=f"flow:result:{task.user_id}:{outcome}")]
    rows = [row]
    if comment_action is not None and comment_action.has_comments:
        comments = task.comment_result
        if comments is None:
            if outcome == "success":
                row.append(comment_button(comment_action, task.user_id))
        else:
            label = "⚠️ 评论未全部提取" if comments.incomplete else "✅ 评论提取完成"
            row.append(
                InlineKeyboardButton(label, callback_data=f"flow:result:{task.user_id}:comments")
            )
            button = comment_button(comment_action, task.user_id)
            button.text = "🔄 重新提取评论"
            rows.append([button])
    return InlineKeyboardMarkup(rows)


def is_task_result(message):
    from pyrogram.types import InlineKeyboardMarkup

    markup = getattr(message, "reply_markup", None)
    return isinstance(markup, InlineKeyboardMarkup) and any(
        str(button.callback_data).startswith("flow:result:")
        for row in markup.inline_keyboard
        for button in row
    )


class TaskStatus:
    """每次阶段/字节进度更新都保留数量与停止按钮。"""

    def __init__(self, message, task):
        self.message, self.task = message, task
        self.unit = task.active_unit if task is not None else None
        self.outcome = None
        self.comment_action = None
        self.terminal_rendered = False
        self._mutation = asyncio.Lock()

    @classmethod
    def wrap(cls, message, task):
        return message if isinstance(message, cls) else cls(message, task)

    def __getattr__(self, name):
        return getattr(self.message, name)

    @property
    def terminal_outcome(self):
        return self.outcome

    def _can_render(self):
        from tgforward.runtime import lifecycle

        if self.task is None:
            return True
        if self.unit is not None and self.task.active_unit is not self.unit:
            return False
        if self.task.cancel_reason == CancelReason.REVOKED:
            return False
        permit = getattr(self.task, "lifecycle_permit", None)
        if permit is not None:
            try:
                lifecycle.assert_current(permit)
            except lifecycle.StalePermit:
                return False
        return self.task.user_id not in lifecycle.revoked

    async def edit(self, text, **kwargs):
        async with self._mutation:
            if (
                self.outcome is not None
                or not self._can_render()
                or self.unit is not None
                and self.task.active_unit is not self.unit
            ):
                return self
            kwargs["reply_markup"] = None if self.task.cancelled else stop_keyboard(self.task)
            await self.message.edit(text + "\n\n" + self.task.media_progress(), **kwargs)
        return self

    async def finish(self, text, outcome="success"):
        async with self._mutation:
            unit = self.unit
            results = [r for r in unit.message_results if not r.is_comment] if unit else []
            if results and self.outcome is None:
                outcomes = {r.outcome for r in results}
                resolved = (
                    "uncertain"
                    if "uncertain" in outcomes
                    else "success"
                    if outcomes == {"success"} and unit.request_current >= unit.request_total
                    else "failed"
                    if outcomes == {"failed"} and unit.request_current >= unit.request_total
                    else "incomplete"
                )
                if resolved != outcome:
                    heading = {
                        "success": "✅ 消息提取完成",
                        "failed": "⚠️ 消息提取失败",
                        "incomplete": "⚠️ 消息提取未完成",
                        "uncertain": "⚠️ 消息发送结果无法确认",
                    }[resolved]
                    text = heading + "\n" + text
                outcome = resolved
                delivered = sum(len(r.delivery.confirmed_delivered) for r in results)
                total = sum(r.delivery.total_parts for r in results)
                if outcome in ("incomplete", "uncertain"):
                    text += (
                        f"\n已确认发送 {delivered}/{total} 部分。重新提取可能造成重复发送。"
                        if total
                        else "\n尚未完成源消息解析。"
                    )
                if self.task.comment_result is not None and "💬 评论提取" not in text:
                    text += "\n\n💬 评论提取\n" + (
                        unit.comments_summary() or self.task.comment_result.summary()
                    )
            if self.outcome is None:
                self.outcome = outcome
            if self.outcome != outcome or self.terminal_rendered or not self._can_render():
                return self
            with contextlib.suppress(MessageNotModified):
                await self.message.edit(
                    text + "\n\n" + self.task.media_progress(),
                    reply_markup=(
                        None
                        if self.task.cancel_reason == CancelReason.SHUTDOWN
                        else result_keyboard(self.task, outcome, self.comment_action)
                    ),
                )
            self.terminal_rendered = True
        return self


def _format_eta(seconds: int) -> str:
    """ETA：不足 1 小时显示 MM:SS，超过 1 小时显示 H:MM:SS（大文件场景常见）。"""
    hours, rest = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _interval_for(total: int) -> int:
    if total >= 100 * 1024 * 1024:
        return 10
    if total >= 50 * 1024 * 1024:
        return 20
    if total >= 10 * 1024 * 1024:
        return 30
    return 50


def make_progress(
    bot_client,
    chat_id: int,
    status_msg_id: int,
    task: Task | None = None,
    label: str = "⬆️ 上传中",
    status_message=None,
    role=SideEffectRole.DOWNLOAD,
):
    """生成 Pyrogram progress 回调。label 用于区分下载/上传阶段。"""

    last_current = -1
    last_step = None  # 回调局部状态，失败/取消后随回调释放；不留全局 key。

    async def on_progress(current: int, total: int) -> None:
        nonlocal last_current, last_step
        if (
            task is not None
            and task.cancel_requested
            and (
                role != SideEffectRole.FINAL_DELIVERY
                or task.cancel_reason not in (None, CancelReason.USER)
            )
        ):
            raise StopTransmission()
        if task is not None and current != last_current:
            task.touch(label)
        last_current = current

        if total <= 0:
            return
        percent = current / total * 100
        interval = _interval_for(total)
        step = int(percent // interval) * interval

        if last_step != step or percent >= 100:
            last_step = step

            filled = int(percent / 10)
            bar = "🟢" * filled + "🔴" * (10 - filled)
            elapsed = time.monotonic() - _start
            speed = current / elapsed / (1024 * 1024) if elapsed > 0 else 0
            if speed > 0:
                eta = _format_eta(int((total - current) / (speed * 1024 * 1024)))
            else:
                eta = "00:00"

            mb = 1048576
            text = (
                f"**{label}...**\n\n{bar}\n\n"
                f"**完成度：** {current / mb:.2f} MB / {total / mb:.2f} MB（{percent:.2f}%）\n"
                f"**速度：** {speed:.2f} MB/s\n"
                f"**剩余时间：** {eta}"
            )
            with contextlib.suppress(Exception):
                if status_message is not None:
                    status = TaskStatus.wrap(status_message, task) if task else status_message
                    await status.edit(text)
                else:
                    await bot_client.edit_message_text(
                        chat_id,
                        status_msg_id,
                        text + "\n\n" + task.media_progress() if task else text,
                        reply_markup=stop_keyboard(task) if task else None,
                    )

            if percent >= 100:
                last_step = None

    _start = time.monotonic()
    return on_progress
