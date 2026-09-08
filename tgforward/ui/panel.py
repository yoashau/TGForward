"""菜单与表单共用一条消息；版本标记阻止旧请求覆盖新页面。"""

import asyncio
import logging
from weakref import WeakValueDictionary

from pyrogram.errors import MessageIdInvalid, MessageNotModified
from pyrogram.types import InlineKeyboardMarkup

logger = logging.getLogger(__name__)
_panels = {}
_persistent = WeakValueDictionary()


def message_key(message):
    message = getattr(message, "_message", message)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    mid = getattr(message, "id", None)
    if isinstance(chat_id, int) and isinstance(mid, int):
        return chat_id, mid
    return None


def is_persistent(message):
    return message_key(message) in _persistent


_keyboards_removed = set()


class Panel:
    def __init__(self, uid, message=None):
        self.uid = uid
        self._message = None
        self.message = message
        self.parent = "home"
        self.markup = None
        self.revision = 0
        self.closed = False
        self.lock = asyncio.Lock()

    @property
    def message(self):
        return self._message

    @message.setter
    def message(self, message):
        old_key = message_key(self._message)
        if old_key is not None and _persistent.get(old_key) is self:
            _persistent.pop(old_key, None)
        self._message = getattr(message, "_message", message)
        key = message_key(self._message)
        if key is not None:
            _persistent[key] = self

    async def render(self, source, text, markup=None, *, revision=None, navigation="back"):
        if navigation not in ("back", "cancel", "none", "preserve"):
            raise ValueError("invalid navigation")
        from tgforward.handlers.menu import keyboard

        async with self.lock:
            if self.closed or (revision is not None and revision != self.revision):
                return self.message
            if navigation == "preserve":
                markup = self.markup
            elif navigation == "none":
                markup = markup if isinstance(markup, InlineKeyboardMarkup) else None
            elif navigation == "cancel":
                from tgforward.ui.dialogue import cancel_keyboard

                markup = markup or cancel_keyboard(self.uid)
            elif not isinstance(markup, InlineKeyboardMarkup) or any(
                str(b.callback_data).startswith("flow:")
                for row in markup.inline_keyboard
                for b in row
            ):
                markup = keyboard([], self.parent)
            self.markup = markup
            # 同一版式，不用隐藏填充字符伪造尺寸；实际宽度仍由 Telegram 客户端排版。
            text = text.strip()
            if self.message is not None:
                try:
                    await self.message.edit(
                        text, reply_markup=markup, disable_web_page_preview=True
                    )
                except MessageNotModified:
                    pass
                except MessageIdInvalid:
                    self.message = None
            if self.message is None:
                raw_source = getattr(source, "_message", source)
                self.message = await raw_source.reply(
                    text, quote=False, reply_markup=markup, disable_web_page_preview=True
                )
            return self.message

    async def close(self):
        self.closed = True
        self.revision += 1
        async with self.lock:
            if self.message:
                try:
                    await self.message.delete()
                except Exception as exc:
                    logger.warning("关闭菜单清理失败 type=%s", type(exc).__name__)
            self.message = None
        if _panels.get(self.uid) is self:
            _panels.pop(self.uid, None)

    async def relocate(self):
        """显式菜单命令重新置底；内部导航不调用，也不扫描聊天记录。"""
        async with self.lock:
            if self.message is not None:
                try:
                    await self.message.delete()
                except MessageIdInvalid:
                    pass
                except Exception as exc:
                    logger.warning("旧菜单清理失败 type=%s", type(exc).__name__)
                self.message = None


def acquire(uid, message=None):
    panel = _panels.get(uid)
    if panel is None or panel.closed:
        panel = _panels[uid] = Panel(uid, message)
    return panel


async def shutdown():
    for panel in list(_panels.values()):
        await panel.close()


async def dismiss_keyboard(message):
    """移除聊天中的底部回复键盘，菜单使用内联按钮。"""
    from pyrogram.types import ReplyKeyboardRemove

    uid = message.from_user.id
    if uid in _keyboards_removed:
        return
    raw_message = getattr(message, "_message", message)
    sent = await raw_message.reply(
        "已收起底部按钮。",
        quote=False,
        reply_markup=ReplyKeyboardRemove(),
    )
    try:
        await sent.delete()
    except Exception as exc:
        logger.warning("旧键盘提示清理失败 type=%s", type(exc).__name__)
        if getattr(message, "_messages", None):
            message._messages.add(sent)
    _keyboards_removed.add(uid)
