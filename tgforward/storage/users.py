"""用户数据读写（SQLite users 表，一个用户一个文档）。

持久化字段：
    user_id, is_whitelisted, session_string(加密), bot_token(加密),
    caption, chat_id, rename_tag, delete_words, replacement_words, updated_at
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from tgforward.config import OWNER_ID
from tgforward.runtime import lifecycle, tasks
from tgforward.storage import sqlite as storage
from tgforward.storage.crypto import decrypt, encrypt

logger = logging.getLogger(__name__)

# 白名单检查的进程内缓存（秒），避免每条消息都打一次数据库
_WHITELIST_TTL = 30
_whitelist_cache: dict[int, tuple[bool, float]] = {}


@dataclass
class UserSettings:
    """一次提取所需的全部用户个性化设置（随用户文档一次取回）。"""

    user_id: int
    caption: str = ""
    chat_id: str = ""  # 原始目标串，如 "-100123/5"；空表示发回用户当前会话
    rename_tag: str = ""
    delete_words: list[str] = field(default_factory=list)
    replacements: dict[str, str] = field(default_factory=dict)
    auto_comments: bool = False  # 提取正文后自动提取评论文字与媒体附件


async def get_user(user_id: int) -> dict | None:
    try:
        return await storage.get_user(int(user_id))
    except Exception as e:
        logger.error("读取用户数据出错 %s: %s", user_id, e)
        return None


async def get_field(user_id: int, key: str, default=None):
    """读取用户的单个字段。"""
    doc = await get_user(user_id)
    return doc.get(key, default) if doc else default


async def set_field(user_id: int, key: str, value) -> bool:
    try:

        def update(doc):
            _set_path(doc, key, value)
            doc["updated_at"] = datetime.now()

        await storage.mutate_user(int(user_id), update)
        return True
    except Exception as e:
        logger.error("写入用户字段出错 %s.%s: %s", user_id, key, e)
        return False


async def unset_field(user_id: int, key: str) -> bool:
    try:
        await storage.mutate_user(int(user_id), lambda doc: _unset_path(doc, key), create=False)
        return True
    except Exception as e:
        logger.error("移除用户字段出错 %s.%s: %s", user_id, key, e)
        return False


async def load_user_settings(user_id: int) -> UserSettings:
    """一次取回用户文档并组装设置对象，避免处理链路中多次查库。"""
    doc = await get_user(user_id)
    doc = doc or {}
    return UserSettings(
        user_id=int(user_id),
        caption=doc.get("caption") or "",
        chat_id=str(doc.get("chat_id") or ""),
        rename_tag=doc.get("rename_tag") or "",
        delete_words=list(doc.get("delete_words") or []),
        replacements=dict(doc.get("replacement_words") or {}),
        auto_comments=bool(doc.get("auto_comments", False)),
    )


# ─── 会话凭证（AES-GCM 加密存储）─────────────────────────────────────────────


async def _credential_change(user_id, field, value):
    from tgforward.telegram import clients

    async with lifecycle.user_lock(user_id):
        if tasks.is_active(user_id):
            return False
        if value is not None and user_id in lifecycle.revoked:
            return False
        ok = (
            await unset_field(user_id, field)
            if value is None
            else await set_field(user_id, field, value)
        )
        if ok:
            remove = (
                clients.remove_user_client
                if field == "session_string"
                else clients.remove_helper_bot
            )
            await remove(user_id)
        return ok


async def save_session(user_id: int, session_string: str) -> bool:
    return await _credential_change(user_id, "session_string", encrypt(session_string))


async def get_session(user_id: int) -> str | None:
    """返回已解密的 session string，未登录返回 None。"""
    doc = await get_user(user_id)
    stored = doc.get("session_string") if doc else None
    if not stored:
        return None
    try:
        return decrypt(stored)
    except Exception as e:
        logger.error("解密会话失败 %s（密钥是否变更？）: %s", user_id, e)
        return None


async def remove_session(user_id: int) -> bool:
    return await _credential_change(user_id, "session_string", None)


# ─── 辅助 bot 令牌（AES-GCM 加密存储）────────────────────────────────────────


async def save_helper_token(user_id: int, bot_token: str) -> bool:
    return await _credential_change(user_id, "bot_token", encrypt(bot_token))


async def get_helper_token(user_id: int) -> str | None:
    """返回已解密的辅助 bot 令牌，未绑定返回 None。

    导入的明文 Token 经格式验证后也可以读取。
    """
    doc = await get_user(user_id)
    stored = doc.get("bot_token") if doc else None
    if not stored:
        return None
    try:
        return decrypt(stored)
    except Exception:
        if isinstance(stored, str) and re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", stored):
            logger.info("用户 %s 的辅助 bot 令牌为明文，建议重新 /bindbot", user_id)
            return stored
        logger.error("解密辅助 bot 令牌失败 %s", user_id)
        return None


async def remove_helper_token(user_id: int) -> bool:
    return await _credential_change(user_id, "bot_token", None)


# ─── 白名单 ──────────────────────────────────────────────────────────────────


async def incr_stats(user_id: int, key: str, amount: int = 1) -> None:
    """累加用户统计（stats.<key>）。统计非关键路径，失败静默。"""
    try:

        def update(doc):
            stats = doc.setdefault("stats", {})
            stats[key] = stats.get(key, 0) + amount

        await storage.mutate_user(int(user_id), update, create=False)
    except Exception as e:
        logger.debug("统计写入失败 %s.%s: %s", user_id, key, e)


RECENT_COMMIT_LIMIT = 256


async def record_extract_success(
    user_id, chat_ref, message_id, is_private, summary, commit_key, *, permit=None
):
    """在同一事务内幂等写入成功次数、历史及有界提交键。"""
    if not commit_key:
        raise ValueError("commit_key is required")
    entry = {
        "c": chat_ref,
        "m": message_id,
        "p": is_private,
        "s": summary[:40],
        "t": datetime.now(),
    }

    def update(doc):
        if permit is not None:
            lifecycle.assert_current(permit)
        keys = doc.get("recent_commit_keys") or []
        if commit_key in keys:
            return "already_committed"
        stats = doc.setdefault("stats", {})
        stats["extracts"] = stats.get("extracts", 0) + 1
        doc["history"] = [*(doc.get("history") or []), entry][-20:]
        doc["recent_commit_keys"] = [*keys, commit_key][-RECENT_COMMIT_LIMIT:]
        return "committed"

    async with lifecycle.user_lock(user_id):
        if permit is not None:
            lifecycle.assert_current(permit)
        result = await storage.mutate_user(int(user_id), update, create=False)
    return result if result is not None else "user_missing"


async def add_history(
    user_id: int, chat_ref: str, message_id: int, is_private: bool, summary: str
) -> None:
    """记录一条提取历史（每个用户保留最近 20 条）。"""
    entry = {
        "c": chat_ref,
        "m": message_id,
        "p": is_private,
        "s": summary[:40],
        "t": datetime.now(),
    }
    try:

        def update(doc):
            doc["history"] = [*(doc.get("history") or []), entry][-20:]

        await storage.mutate_user(int(user_id), update, create=False)
    except Exception as e:
        logger.debug("历史写入失败 %s: %s", user_id, e)


async def set_whitelisted(user_id: int, value: bool) -> bool:
    async with lifecycle.operation(user_id), lifecycle.user_lock(user_id):
        ok = await set_field(user_id, "is_whitelisted", value)
        if ok:
            _whitelist_cache[int(user_id)] = (value, time.monotonic())
            if value:
                lifecycle.activate(user_id)
            else:
                lifecycle.revoke(user_id)
        return ok


async def delete_user(user_id: int) -> bool:
    """撤销后等待任务退出，再串行清理用户资源。"""
    from tgforward.telegram import clients
    from tgforward.ui import dialogue

    async with lifecycle.operation(user_id):
        generation = lifecycle.revoke(user_id)
        task = tasks.get(user_id)
        tasks.request_cancel(user_id, tasks.CancelReason.REVOKED)
        if task is not None and task.runner is not None:
            await asyncio.gather(task.runner, return_exceptions=True)
        async with lifecycle.user_lock(user_id):
            if not lifecycle.validate_cleanup(user_id, generation):
                return False
            try:
                await storage.delete_user(int(user_id))
            except Exception as exc:
                logger.error("删除用户失败 user=%s type=%s", user_id, type(exc).__name__)
                return False
            _whitelist_cache[int(user_id)] = (False, time.monotonic())
            await dialogue.clear(user_id)
            await clients.remove_user_client(user_id)
            await clients.remove_helper_bot(user_id)
            from tgforward.transfers import delivery

            delivery.purge_user(user_id)
            from tgforward.handlers import router

            router.purge_user(user_id)
            if task is not None:
                tasks.finish(user_id, task)
            tasks._last_finished.pop(user_id, None)
            from tgforward.ui import panel

            screen = panel._panels.pop(user_id, None)
            if screen is not None:
                screen.closed = True
                screen.message = None
            panel._keyboards_removed.discard(user_id)
            from tgforward.utils.media import remove_custom_thumb

            return remove_custom_thumb(user_id) != "failed"


async def is_whitelisted(user_id: int) -> bool:
    user_id = int(user_id)
    if user_id in OWNER_ID:
        return True

    cached = _whitelist_cache.get(user_id)
    if cached and time.monotonic() - cached[1] < _WHITELIST_TTL:
        return cached[0]

    try:
        doc = await storage.get_user(user_id)
        allowed = bool(doc and doc.get("is_whitelisted", False))
    except Exception as e:
        logger.error("检查白名单出错 %s: %s", user_id, e)
        return False

    _whitelist_cache[user_id] = (allowed, time.monotonic())
    return allowed


class RuleConflict(ValueError):
    """同一原词同时出现在替换和删除规则中。"""


async def _mutate_fields(user_id, mutate):
    """在 BEGIN IMMEDIATE 内读改写；读取失败绝不当成空配置。"""

    def update(doc):
        values = mutate(doc)
        doc.update(values)
        doc["updated_at"] = datetime.now()
        return values

    try:
        return await storage.mutate_user(int(user_id), update)
    except RuleConflict:
        raise
    except Exception as exc:
        logger.error("原子更新设置失败 user=%s type=%s", user_id, type(exc).__name__)
        return None


async def update_word_rules(user_id, *, replacement=None, delete_words=()):
    def merge(doc):
        replacements = dict(doc.get("replacement_words") or {})
        words = list(dict.fromkeys([*(doc.get("delete_words") or []), *delete_words]))
        if replacement is not None:
            word, value = replacement
            replacements[word] = value
        conflicts = set(words).intersection(replacements)
        if conflicts:
            raise RuleConflict("、".join(sorted(conflicts)))
        return {"delete_words": words, "replacement_words": replacements}

    return await _mutate_fields(user_id, merge) is not None


async def toggle_auto_comments(user_id):
    result = await _mutate_fields(
        user_id,
        lambda doc: {"auto_comments": not doc.get("auto_comments", False)},
    )
    return result["auto_comments"] if result is not None else None


async def reset_settings(user_id):
    try:

        def reset(doc):
            for key in (
                "delete_words",
                "replacement_words",
                "rename_tag",
                "caption",
                "chat_id",
                "auto_comments",
                "transfer_mode",
            ):
                doc.pop(key, None)

        await storage.mutate_user(int(user_id), reset, create=False)
        return True
    except Exception as exc:
        logger.error("重置设置失败 user=%s type=%s", user_id, type(exc).__name__)
        return False


def _logout_key(session_string):
    import hashlib

    return hashlib.sha256(session_string.encode()).hexdigest()


async def record_logout_pending(user_id, session_string, error):
    # 不再作为活动凭据使用；仅 /logout 重试路径读取，仍使用 AES-GCM 加密。
    return await set_field(
        user_id,
        f"pending_logouts.{_logout_key(session_string)}",
        {
            "session_string": encrypt(session_string),
            "error": error,
            "updated_at": datetime.now(),
        },
    )


async def clear_logout_pending(user_id, session_string):
    return await unset_field(user_id, f"pending_logouts.{_logout_key(session_string)}")


async def get_pending_logouts(user_id):
    doc = await storage.get_user(int(user_id)) or {}
    return [
        decrypt(entry["session_string"]) for entry in (doc.get("pending_logouts") or {}).values()
    ]


def _set_path(doc, key, value):
    parts = key.split(".")
    for part in parts[:-1]:
        doc = doc.setdefault(part, {})
    doc[parts[-1]] = value


def _unset_path(doc, key):
    parts = key.split(".")
    for part in parts[:-1]:
        doc = doc.get(part)
        if not isinstance(doc, dict):
            return
    doc.pop(parts[-1], None)


async def list_whitelisted_ids():
    return await storage.list_whitelisted_ids()


async def count_whitelisted(*, exclude_owners=False):
    return await storage.count_whitelisted(OWNER_ID if exclude_owners else ())


async def list_whitelisted_page(offset, limit, *, exclude_owners=False):
    return await storage.list_whitelisted_ids(offset, limit, OWNER_ID if exclude_owners else ())


async def storage_healthy():
    return await storage.healthcheck()
