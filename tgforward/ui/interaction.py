"""临时交互消息按 TTL 清理；持久菜单由 Panel 独立持有。"""

import asyncio
import logging
from contextvars import ContextVar
from functools import wraps

logger = logging.getLogger(__name__)
FEEDBACK_TTL = 30  # 保留时间供用户阅读；不让失败提示一闪而过
_current = ContextVar("interaction", default=None)
_pending = set()
_owners = {}


class Messages:
    def __init__(self):
        self.items = {}
        self.deferred = False
        self.delay = FEEDBACK_TTL

    def add(self, message):
        from tgforward.ui.panel import is_persistent

        message = getattr(message, "_message", message)
        if is_persistent(message):
            return
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        mid = getattr(message, "id", None)
        if isinstance(chat_id, int) and isinstance(mid, int):
            key = (chat_id, mid)
            previous = _owners.get(key)
            if previous is not None and previous is not self:
                previous.items.pop(key, None)
            _owners[key] = self
            self.items[key] = message

    async def delete(self):
        from tgforward.ui.panel import is_persistent

        items, self.items = self.items, {}
        for (chat_id, mid), message in items.items():
            key = (chat_id, mid)
            if _owners.get(key) is not self:
                continue
            _owners.pop(key, None)
            if is_persistent(message):
                continue
            try:
                await asyncio.wait_for(message.delete(), timeout=5)
            except Exception as exc:
                logger.warning(
                    "交互消息清理失败 chat=%s message=%s type=%s", chat_id, mid, type(exc).__name__
                )

    def later(self, delay=None):
        delay = self.delay if delay is None else delay

        async def clean():
            await asyncio.sleep(delay)
            await self.delete()

        job = asyncio.create_task(clean())
        _pending.add(job)
        job.add_done_callback(_pending.discard)


class MessageView:
    """委托原始消息 API，仅把 reply 的返回值登记为临时 UI。"""

    def __init__(self, message, messages, actor=None, command=None):
        self._message = getattr(message, "_message", message)
        self._messages = messages
        self.from_user = actor or getattr(message, "from_user", None)
        self.command = command if command is not None else getattr(message, "command", None)
        self._panel = getattr(message, "_panel", None)
        self._revision = getattr(message, "_revision", None)

    def __getattr__(self, name):
        return getattr(self._message, name)

    async def delete(self):
        from tgforward.ui.panel import is_persistent

        if is_persistent(self._message):
            return  # 菜单仅由 Panel.close/relocate 删除，不依赖消息对象身份。
        return await self._message.delete()

    async def edit(self, text, **kwargs):
        if self._panel:
            await self._panel.render(
                self, text, kwargs.get("reply_markup"), revision=self._revision
            )
            return self
        return await self._message.edit(text, **kwargs)

    async def reply(self, *args, **kwargs):
        if self._panel:
            result = await self._panel.render(
                self,
                args[0] if args else kwargs["text"],
                kwargs.get("reply_markup"),
                revision=self._revision,
            )
            view = MessageView(result, self._messages)
            view._panel, view._revision = self._panel, self._revision
            return view
        # 输入可能已因凭证保密或上一层菜单清理而删除，不引用它发送新提示。
        kwargs.setdefault("quote", False)
        result = await self._message.reply(*args, **kwargs)
        self._messages.add(result)
        markup = kwargs.get("reply_markup")
        if markup and any(
            str(button.callback_data).startswith(("nav:", "set:"))
            for row in markup.inline_keyboard
            for button in row
        ):
            self._messages.delay = 300
        return MessageView(result, self._messages)


class QueryView:
    def __init__(self, query, messages):
        self._query = query
        self.message = (
            MessageView(query.message, messages, actor=query.from_user) if query.message else None
        )

    def __getattr__(self, name):
        return getattr(self._query, name)


def interaction(func):
    """所有命令/会话/回调共用；后台提取可显式接管同一批临时消息。"""

    @wraps(func)
    async def wrapped(client, update):
        if isinstance(update, (MessageView, QueryView)):
            return await func(client, update)
        from tgforward.ui import state

        uid = getattr(getattr(update, "from_user", None), "id", None)
        st = state.get(uid)
        messages = st.data.get("ui") if st else None
        messages = messages or Messages()
        is_query = hasattr(update, "data") and hasattr(update, "answer")
        if is_query:
            from tgforward.runtime import tasks
            from tgforward.transfers.progress import is_task_result
            from tgforward.ui.panel import message_key

            is_comment = str(update.data).startswith(("cmt:", "flow:cancel:"))
            managed_comment = is_comment and is_task_result(update.message)
            if managed_comment:
                key = message_key(update.message)
                active = tasks.get(uid)
                if active and active.comment_target == key:
                    # 重复点击或停止按钮不从评论任务抢走清理 ownership。
                    messages = _owners.get(key) or messages
            view = QueryView(update, messages)
            # 菜单独立持有；手动评论接管管理消息，后台任务结束才重新计时。
            if update.message and (
                managed_comment or not str(update.data).startswith(("nav:", "set:", "cmt:"))
            ):
                messages.add(update.message)
        else:
            messages.add(update)
            view = MessageView(update, messages)
        from tgforward.ui import panel as panels

        command = (getattr(update, "command", None) or [""])[0]
        is_menu = (
            (is_query and str(update.data).startswith(("nav:", "set:")))
            or (
                not is_query
                and command
                in {
                    "start",
                    "help",
                    "setting",
                    "account",
                    "login",
                    "logout",
                    "bindbot",
                    "unbindbot",
                    "allow",
                    "ban",
                    "me",
                    "history",
                    "status",
                    "whitelist",
                    "cancel",
                    "cancle",
                    "id",
                    "set",
                }
            )
            or (not is_query and getattr(update, "text", None) == "📋 菜单")
            or (st is not None and st.data.get("panel") is not None)
        )
        if is_menu and uid is not None:
            candidate = update.message if is_query else None
            private_id = getattr(getattr(candidate or update, "chat", None), "id", None)
            if private_id == uid:
                panel = panels.acquire(uid, candidate)
                if not is_query and command in {"start", "setting", "account"}:
                    await panel.relocate()
                # 每次导航使旧异步处理失效，表单的连续输入仍使用同一版本。
                if is_query or command or getattr(update, "text", None) == "📋 菜单":
                    panel.revision += 1
                if not is_query and command:
                    panel.parent = {
                        "start": None,
                        "help": None,
                        "setting": "home",
                        "account": "home",
                        "login": "telegram",
                        "logout": "telegram",
                        "bindbot": "helper",
                        "unbindbot": "helper",
                        "allow": "list:0",
                        "ban": "list:0",
                        "me": "account",
                        "history": "account",
                        "status": "admin",
                        "whitelist": "admin",
                        "id": "account",
                        "set": "admin",
                    }.get(command, panel.parent)
                target = view.message if is_query else view
                if target:
                    target._panel, target._revision = panel, panel.revision
                if not is_query:
                    try:
                        await view.delete()
                    except Exception as exc:
                        logger.warning("输入清理失败 user=%s type=%s", uid, type(exc).__name__)
        token = _current.set(messages)
        try:
            return await func(client, view)
        finally:
            _current.reset(token)
            active = state.get(uid)
            if active is not None:
                messages.delay = FEEDBACK_TTL
                previous = active.data.get("ui")
                if previous and previous is not messages:
                    messages.items.update(previous.items)
                active.data["ui"] = messages
                target = view.message if isinstance(view, QueryView) else view
                if target and target._panel:
                    active.data["panel"] = target._panel
            elif not messages.deferred:
                messages.later()

    return wrapped


def defer():
    messages = _current.get()
    if messages:
        messages.deferred = True
    return messages


async def shutdown():
    from tgforward.ui import panel

    await panel.shutdown()
    jobs = list(_pending)
    for job in jobs:
        job.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)
    await asyncio.gather(*(bucket.delete() for bucket in set(_owners.values())))
