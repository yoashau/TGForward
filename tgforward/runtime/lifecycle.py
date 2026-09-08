"""凭据读写、客户端创建/删除共享用户级锁；嵌套调用只允许同一协程重入。"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from weakref import WeakValueDictionary

_locks = WeakValueDictionary()  # 持有者和等待者保留强引用，空闲 UID 自动回收。
_owners = {}
revoked = set()


@asynccontextmanager
async def user_lock(uid):
    uid = int(uid)
    task = asyncio.current_task()
    if _owners.get(uid) is task:
        yield
        return
    lock = _locks.setdefault(uid, asyncio.Lock())
    async with lock:
        _owners[uid] = task
        try:
            yield
        finally:
            _owners.pop(uid, None)


def serialized(func):
    """命令与任务登记共享生命周期锁；排队的旧表单输入不进入下一步。"""
    from functools import wraps

    @wraps(func)
    async def wrapped(client, update):
        from tgforward.ui import state

        uid = getattr(getattr(update, "from_user", None), "id", None)
        if uid is None:
            return await func(client, update)
        st = state.get(uid)
        step = st.step if st else None
        is_input = not getattr(update, "command", None) and not hasattr(update, "data")
        async with user_lock(uid):
            if is_input and (state.get(uid) is not st or (st and st.step != step)):
                return
            return await func(client, update)

    return wrapped


# 以下 transition/fence 不含 await；保证单事件循环内的应用层线性化。
# generation 墓碑保留到进程结束，不能因清理缓存使旧许可重新有效。
_generations = {}
_operation_locks = WeakValueDictionary()


class StalePermit(Exception):
    """用户已撤销或许可属于旧生命周期。"""


@dataclass(frozen=True)
class Permit:
    user_id: int
    generation: int


def current_generation(uid):
    return _generations.get(int(uid), 0)


def capture_permit(uid):
    uid = int(uid)
    if uid in revoked:
        raise StalePermit("user lifecycle is revoked")
    return Permit(uid, current_generation(uid))


def assert_current(permit):
    if permit.user_id in revoked or permit.generation != current_generation(permit.user_id):
        raise StalePermit("user lifecycle permit is stale")


def revoke(uid):
    uid = int(uid)
    generation = current_generation(uid) + 1
    _generations[uid] = generation
    revoked.add(uid)
    return generation


def activate(uid):
    uid = int(uid)
    if uid in revoked:
        _generations[uid] = current_generation(uid) + 1
        revoked.remove(uid)
    return capture_permit(uid)


def validate_cleanup(uid, generation):
    uid = int(uid)
    return uid in revoked and current_generation(uid) == generation


@asynccontextmanager
async def operation(uid):
    """仅生命周期管理路径获取；runner 不获取，可跨 await runner 持有。"""
    lock = _operation_locks.setdefault(int(uid), asyncio.Lock())
    async with lock:
        yield


def authorize_side_effect(task, role):
    from tgforward.transfers.results import SideEffectRole

    SideEffectRole(role)  # 未知 role 不能获得发送许可。
    task.check_cancel()
    permit = task.lifecycle_permit
    if permit is None:
        permit = task.lifecycle_permit = capture_permit(task.user_id)
    assert_current(permit)


def authorize_new_attempt(task, state, parts, role):
    """检查整组后才允许调用方 begin；检查失败不会留下部分 in-flight。"""
    authorize_side_effect(task, role)
    for part in parts:
        state.authorize(part, role)
