"""手动评论任务状态：原地更新管理消息，保留原帖结果及按钮。"""

import asyncio
import logging
from types import SimpleNamespace

from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tgforward.transfers.progress import TaskStatus
from tgforward.utils.text import (
    RichText,
    append_text,
    entity_kwargs,
    message_text,
    truncate_text,
    utf16_len,
)

logger = logging.getLogger(__name__)
LABELS = {
    "running": "⏹ 停止提取评论",
    "success": "✅ 评论已提取",
    "partial": "⚠️ 部分未完成 · 重新提取",
    "failed": "⚠️ 提取失败 · 重新提取",
    "stopped": "⏹ 已停止 · 重新提取",
}
DETAILS = {
    "running": "🔎 正在读取评论区……",
    "success": "评论提取完成。",
    "partial": "评论尚未全部提取，可重新提取，已发送评论可能重复。",
    "failed": "评论提取失败，可重新提取，已发送评论可能重复。",
    "stopped": "评论提取已停止，可重新提取，已发送评论可能重复。",
}
SECTION = "\n\n💬 评论提取\n"


class CommentButton(TaskStatus):
    """TaskStatus 的评论视图；搬运层 wrap 不再套用原帖的停止/结果键盘。"""

    def __init__(self, client, message, markup=None, *, task=None):
        super().__init__(message, task)
        self.client = client
        self.chat = SimpleNamespace(id=getattr(getattr(message, "chat", None), "id", None))
        self.id = getattr(message, "id", None)
        self.markup = markup or getattr(message, "reply_markup", None)
        self.original_markup = self.markup
        self.key = (self.chat.id, self.id)
        original = message_text(message)
        # 重试替换上一次评论状态，不把它并入原帖结果、反复追加。
        base = str(original).rsplit(SECTION, 1)[0]
        self.base_text = RichText(base, original.entities)
        self.lock = asyncio.Lock()

    def _keyboard(self, outcome):
        if not isinstance(self.original_markup, InlineKeyboardMarkup):
            return self.original_markup
        rows, rerun = [], []
        for row in self.original_markup.inline_keyboard:
            output = []
            for button in row:
                if not str(button.callback_data).startswith("cmt:"):
                    output.append(button)
                    continue
                callback = button.callback_data
                if outcome == "running" and self.task is not None:
                    callback = f"flow:cancel:{self.task.user_id}:{self.task.token}"
                elif outcome == "success":
                    owner = (
                        self.task.user_id if self.task is not None else str(callback).split(":")[-1]
                    )
                    rerun.append(InlineKeyboardButton("🔄 重新提取评论", callback_data=callback))
                    callback = f"flow:result:{owner}:comments_success"
                output.append(InlineKeyboardButton(LABELS[outcome], callback_data=callback))
            rows.append(output)
        if rerun:
            rows.append(rerun)
        return InlineKeyboardMarkup(rows)

    async def _render(self, text):
        # 字节进度带有加粗标记；评论区域使用纯文本，原帖实体独立保留。
        detail = str(text).replace("**", "")
        if detail.startswith("评论提取："):
            detail = detail.removeprefix("评论提取：")
        if self.task is not None:
            detail += "\n\n" + self.task.media_progress()
        section = truncate_text("💬 评论提取\n" + detail, 1600)
        base = truncate_text(self.base_text, 4096 - utf16_len(section) - 2)
        body = append_text(base, section)

        async def render():
            if not self._can_render():
                return False
            await self.client.edit_message_text(
                self.chat.id,
                self.id,
                body,
                reply_markup=(
                    None
                    if self.task is not None
                    and self.task.cancel_reason is not None
                    and self.task.cancel_reason.name == "SHUTDOWN"
                    else self.markup
                ),
                disable_web_page_preview=True,
                **entity_kwargs(body),
            )
            return True

        try:
            return await asyncio.wait_for(render(), timeout=5)
        except MessageNotModified:
            pass
        except Exception as exc:
            logger.warning(
                "评论状态更新失败 chat=%s message=%s type=%s",
                self.chat.id,
                self.id,
                type(exc).__name__,
            )
            return False
        return True

    async def set_running(self, text=""):
        async with self.lock:
            if self.outcome is not None or not self._can_render():
                return self
            self.markup = self._keyboard("running")
            await self._render(text or DETAILS["running"])
        return self

    async def finish(self, text="", outcome="success"):
        if outcome == "running":
            return await self.set_running(text)
        async with self.lock:
            if self.outcome is None:
                self.outcome = outcome
            if self.outcome != outcome or self.terminal_rendered or not self._can_render():
                return self
            self.markup = self._keyboard(outcome)
            self.terminal_rendered = await self._render(text or DETAILS[outcome])
        return self

    async def edit(self, text, **kwargs):
        async with self.lock:
            if self.outcome is not None or not self._can_render():
                return self
            self.markup = self._keyboard("running")
            await self._render(text)
        return self
