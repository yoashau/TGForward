"""用户级对话状态注册表（登录流程与设置面板共用）。

每个用户同一时刻最多一个对话状态；路由器通过 `user_busy` 过滤器与
对话中的用户互斥，避免同一条消息被对话处理器和链接路由重复消费。
"""

from dataclasses import dataclass, field
from uuid import uuid4

from pyrogram import filters


@dataclass
class UserState:
    kind: str  # "login" 或 "settings"
    step: str  # login: phone/code/password；settings: 具体设置项
    data: dict = field(default_factory=dict)
    token: str = field(default_factory=lambda: uuid4().hex[:12])


_states: dict[int, UserState] = {}


def get(user_id: int) -> UserState | None:
    return _states.get(user_id)


def set(user_id: int, kind: str, step: str, **data) -> UserState:
    st = UserState(kind=kind, step=step, data=dict(data))
    _states[user_id] = st
    return st


def set_step(user_id: int, step: str) -> None:
    st = _states.get(user_id)
    if st:
        st.step = step


def update(user_id: int, **data) -> None:
    st = _states.get(user_id)
    if st:
        st.data.update(data)


def clear(user_id: int) -> None:
    _states.pop(user_id, None)


def is_busy(user_id: int) -> bool:
    return user_id in _states


# ─── Pyrogram 过滤器 ─────────────────────────────────────────────────────────


def _kind_filter(kind: str):
    def func(_, __, update):
        uid = getattr(update.from_user, "id", None)
        st = _states.get(uid) if uid is not None else None
        return st is not None and st.kind == kind

    return filters.create(func)


login_active = _kind_filter("login")
settings_active = _kind_filter("settings")


def _busy_filter(_, __, message):
    uid = getattr(message.from_user, "id", None)
    return uid is not None and uid in _states


user_busy = filters.create(_busy_filter)

# 对话输入统一排除命令，让 /start /cancel 等始终能到达命令处理器。
non_command = ~filters.regex(r"^\s*/|^📋 菜单$")
helper_active = _kind_filter("helper")
admin_active = _kind_filter("admin")
