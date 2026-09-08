"""所有 Telegram 客户端的创建与生命周期管理。

- bot:       主机器人（Bot Token 登录），指令处理与常规发送/克隆
- premium:   Premium 账号会话（可选），仅用于 >2GB 大文件上传
- 用户登录会话客户端 / 辅助 bot 客户端：按需启动并缓存，登出/解绑时停止

全部客户端使用 in_memory 会话，不在磁盘落 session 文件。
"""

import asyncio
import logging
import time

from pyrogram import Client

from tgforward.config import API_HASH, API_ID, BOT_TOKEN, STRING
from tgforward.runtime import lifecycle
from tgforward.storage import users
from tgforward.telegram.compat import install

install()

logger = logging.getLogger(__name__)

_STARTED_AT = time.monotonic()


def uptime_text() -> str:
    """进程运行时长（供 /status 使用）。"""
    seconds = int(time.monotonic() - _STARTED_AT)
    return f"{seconds // 3600}小时{(seconds % 3600) // 60}分{seconds % 60}秒"


def _client(name: str, **kwargs) -> Client:
    return Client(
        name,
        api_id=API_ID,
        api_hash=API_HASH,
        device_model="TGForward",
        in_memory=True,
        **kwargs,
    )


bot: Client = _client("bot", bot_token=BOT_TOKEN)
premium: Client | None = _client("premium", session_string=STRING) if STRING else None

premium_started: bool = False
_closing = False
_unclosed = {}  # 清理失败仍持有引用，shutdown 再尝试，不留下不可追踪连接

_user_clients: dict[int, Client] = {}
_helper_bots: dict[int, Client] = {}
_dialogs_cached: set[int] = set()  # 已预取过对话列表的用户客户端


async def start_main_clients() -> bool:
    """启动主机器人与 Premium 会话。Premium 失败时降级为 2GB 模式继续运行。

    返回 Premium 通道是否可用。
    """
    global premium_started, _closing

    _closing = False
    await safe_start_client(bot)
    logger.info("主机器人已启动")

    if premium is not None:
        try:
            await safe_start_client(premium)
            premium_started = True
            logger.info("Premium 会话已启动（4GB 上传通道可用）")
        except Exception as e:
            premium_started = False
            logger.warning("Premium 会话启动失败，已降级为 2GB 模式：%s", e)

    return premium_started


async def stop_all_clients() -> None:
    """先关闭创建入口，等待同用户在途操作，再按实际状态回收全部客户端。"""
    global _closing, premium_started
    _closing = True
    for uid in list(set(lifecycle._locks) | set(_user_clients) | set(_helper_bots)):
        async with lifecycle.user_lock(uid):
            await remove_user_client(uid)
            await remove_helper_bot(uid)
    if premium is not None:
        await _stop_quietly(premium, "premium")
    premium_started = False
    await _stop_quietly(bot, "bot")
    for c in list(_unclosed.values()):
        await _stop_quietly(c, "pending-cleanup")
    logger.info("所有客户端清理完成 pending=%s", len(_unclosed))


async def safe_connect_client(c):
    c._tf_cleaned = False
    _unclosed[id(c)] = c
    try:
        return await asyncio.wait_for(c.connect(), timeout=60)
    except BaseException:
        await _stop_quietly(c, "connect-rollback")
        raise


async def safe_start_client(c):
    c._tf_cleaned = False
    _unclosed[id(c)] = c
    try:
        await asyncio.wait_for(c.start(), timeout=60)
        return c
    except BaseException:
        # 包括取消；connect 成功但 initialize 失败的对象尚未进入用户缓存。
        await _stop_quietly(c, "startup-rollback")
        raise


async def _stop_quietly(c: Client, label: str) -> None:
    if c.__dict__.get("_tf_cleaned") and not (
        getattr(c, "is_connected", False) or getattr(c, "is_initialized", False)
    ):
        return
    _unclosed[id(c)] = c
    cleanup_failed = False
    try:
        if getattr(c, "is_initialized", False):
            await asyncio.wait_for(c.stop(), 10)
        else:
            # initialize 可能已创建一部分 worker，却尚未设置 is_initialized。
            dispatcher = getattr(c, "dispatcher", None)
            if dispatcher and getattr(dispatcher, "handler_worker_tasks", None):
                await asyncio.wait_for(dispatcher.stop(), 5)
            for session in list(getattr(c, "media_sessions", {}).values()):
                await asyncio.wait_for(session.stop(), 5)
            if getattr(c, "is_connected", True):
                await asyncio.wait_for(c.disconnect(), 5)
            else:
                # connect 可能在设置 is_connected 之前失败，仍需关闭临时 session/storage。
                if getattr(c, "session", None):
                    await asyncio.wait_for(c.session.stop(), 5)
                if getattr(c, "storage", None) and getattr(c.storage, "conn", True) is not None:
                    await asyncio.wait_for(c.storage.close(), 5)
    except Exception as exc:
        logger.warning("清理客户端 %s 失败 type=%s，尝试底层回收", label, type(exc).__name__)
        try:
            dispatcher = getattr(c, "dispatcher", None)
            workers = list(getattr(dispatcher, "handler_worker_tasks", []) or [])
            for worker in workers:
                worker.cancel()
            if workers:
                await asyncio.wait_for(asyncio.gather(*workers, return_exceptions=True), 5)
            for session in list(getattr(c, "media_sessions", {}).values()):
                await asyncio.wait_for(session.stop(), 5)
            if getattr(c, "session", None):
                await asyncio.wait_for(c.session.stop(), 5)
            if getattr(c, "storage", None) and getattr(c.storage, "conn", True) is not None:
                await asyncio.wait_for(c.storage.close(), 5)
            watcher = getattr(c, "updates_watchdog_task", None)
            if watcher is not None and not watcher.done():
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            c.is_initialized = c.is_connected = False
        except Exception as cleanup_exc:
            cleanup_failed = True
            logger.error("客户端待再次清理 %s type=%s", label, type(cleanup_exc).__name__)
    if (
        not cleanup_failed
        and not getattr(c, "is_connected", False)
        and not getattr(c, "is_initialized", False)
    ):
        c._tf_cleaned = True
        _unclosed.pop(id(c), None)


# ─── 每用户客户端 ────────────────────────────────────────────────────────────


async def _get_client(user_id, registry, read_secret, prefix, field):
    from tgforward.telegram.wait import current_task

    task = current_task()
    async with lifecycle.user_lock(user_id):
        if task is not None and task.user_id == user_id:
            task.check_cancel()
            lifecycle.assert_current(task.lifecycle_permit)
        if _closing or user_id in lifecycle.revoked:
            return None
        if user_id in registry:
            return registry[user_id]
        permit = lifecycle.capture_permit(user_id)
        # 凭据读取与客户端缓存共享用户锁。
        secret = await read_secret(user_id)
        if not secret:
            return None
        try:
            lifecycle.assert_current(permit)
            c = _client(f"{prefix}:{user_id}", **{field: secret})
            await safe_start_client(c)
        except Exception as exc:
            logger.error("启动 %s 客户端失败 user=%s type=%s", prefix, user_id, type(exc).__name__)
            return None
        if (
            _closing
            or user_id in lifecycle.revoked
            or permit.generation != lifecycle.current_generation(user_id)
        ):
            await _stop_quietly(c, f"{prefix}:{user_id}")
            return None
        registry[user_id] = c
        return c


async def get_user_client(user_id: int) -> Client | None:
    return await _get_client(user_id, _user_clients, users.get_session, "user", "session_string")


async def get_helper_bot(user_id: int) -> Client | None:
    return await _get_client(user_id, _helper_bots, users.get_helper_token, "helper", "bot_token")


async def remove_user_client(user_id: int) -> None:
    async with lifecycle.user_lock(user_id):
        _dialogs_cached.discard(user_id)
        c = _user_clients.pop(user_id, None)
        if c:
            await _stop_quietly(c, f"user:{user_id}")


async def remove_helper_bot(user_id: int) -> None:
    async with lifecycle.user_lock(user_id):
        c = _helper_bots.pop(user_id, None)
        if c:
            await _stop_quietly(c, f"helper:{user_id}")


# ─── 角色化取用 ──────────────────────────────────────────────────────────────


async def ensure_dialogs_cached(user_id: int, client: Client) -> None:
    """预取一次对话列表，加速后续私有频道的 peer 解析（每个客户端仅一次）。"""
    if user_id in _dialogs_cached:
        return
    try:
        async for _ in client.get_dialogs(limit=100):
            pass
        _dialogs_cached.add(user_id)
    except Exception as e:
        logger.warning("预取对话列表失败 %s: %s", user_id, e)


async def get_upload_bot(user_id: int) -> Client:
    """负责对外发送/上传的 bot：优先用户绑定的辅助 bot，否则主机器人。"""
    return await get_helper_bot(user_id) or bot
