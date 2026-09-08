"""最终目标 RPC 的授权边界与同步送达提交。"""

import asyncio

from pyrogram.errors import EntitiesTooLong, EntityBoundsInvalid, FloodWait, RPCError

from tgforward.runtime import lifecycle
from tgforward.transfers.results import SideEffectRole


async def execute(make_call, task, state, parts, *, retryable=(), batch=False):
    parts = tuple(parts)
    if not parts:
        raise ValueError("final delivery requires nonempty parts")
    if task is not None:
        lifecycle.authorize_new_attempt(task, state, parts, SideEffectRole.FINAL_DELIVERY)
    else:
        for part in parts:
            state.authorize(part)
    for part in parts:
        state.begin_attempt(part)
    try:
        result = await make_call()
    except (FloodWait, EntityBoundsInvalid, EntitiesTooLong, *retryable) as exc:
        for part in parts:
            state.retryable_rejection(part, str(exc))
        raise
    except (RPCError, ValueError, TypeError) as exc:
        for part in parts:
            state.reject(part, str(exc))
            state.finalize_failed(part)
        raise
    except (asyncio.CancelledError, Exception) as exc:
        for part in parts:
            state.mark_uncertain(part, str(exc))
        raise
    if batch:
        # 缺失或重复的返回不能建立可靠的源成员位置映射。
        valid = isinstance(result, (list, tuple)) and len(result) == len(parts)
        if valid:
            ids = [getattr(item, "id", None) for item in result]
            valid = all(isinstance(mid, int) and mid > 0 for mid in ids) and len(set(ids)) == len(
                ids
            )
    else:
        valid = result is not None
    for part in parts:
        if valid:
            state.confirm_delivered(part)
        else:
            state.mark_uncertain(part, "RPC response does not confirm the planned delivery")
    return result
