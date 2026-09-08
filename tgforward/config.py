"""环境变量配置的加载与启动校验。

所有值在导入时读取，`validate_config()` 由入口在启动时显式调用，
保证测试环境可以安全导入本模块。
"""

import logging
import math
import os

from dotenv import load_dotenv

load_dotenv(os.getenv("TGFORWARD_ENV_FILE", "/etc/tgforward/tgforward.env"))

logger = logging.getLogger(__name__)

# 拒绝已公开的示例密钥；不作为运行时默认值。
_PUBLIC_MASTER_KEY = "gK8HzLfT9QpViJcYeB5wRa3DmN7P2xUq"
_PUBLIC_SALT = "s7Yx5CpVmE3F"

_PARSE_ERRORS = []


def parse_int(name, default=0):
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        _PARSE_ERRORS.append(f"{name} 必须是整数")
        return default


API_ID: int = parse_int("API_ID")
API_HASH: str = os.getenv("API_HASH", "").strip()
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
DATA_DIR: str = os.path.abspath(os.getenv("DATA_DIR", "").strip() or "/var/lib/tgforward")

OWNER_ID: list[int] = []
for _item in os.getenv("OWNER_ID", "").replace(",", " ").split():
    try:
        _uid = int(_item)
        if _uid <= 0:
            raise ValueError
        OWNER_ID.append(_uid)
    except ValueError:
        _PARSE_ERRORS.append("OWNER_ID 只接受大于 0 的整数用户 ID")
OWNER_ID = list(dict.fromkeys(OWNER_ID))

# Premium 账号 Session String，解锁 4GB 上传
STRING: str | None = os.getenv("STRING", "").strip() or None
_log_group = os.getenv("LOG_GROUP", "").strip()
LOG_GROUP: int | None = parse_int("LOG_GROUP") or None

MASTER_KEY: str = os.getenv("MASTER_KEY", "").strip()
# 其语义是 PBKDF2 盐值；兼容读取旧变量名 IV_KEY
SALT_KEY: str = os.getenv("SALT_KEY", "").strip() or os.getenv("IV_KEY", "").strip()

BATCH_DELAY = max(0, parse_int("BATCH_DELAY", 3))
MAX_CONCURRENT_TRANSFERS = max(1, parse_int("MAX_CONCURRENT_TRANSFERS", 3))
TASK_STALL_TIMEOUT = max(60, parse_int("TASK_STALL_TIMEOUT", 300))
try:
    USER_COOLDOWN = float(os.getenv("USER_COOLDOWN", "3"))
    if not math.isfinite(USER_COOLDOWN) or USER_COOLDOWN < 0:
        raise ValueError
except ValueError:
    _PARSE_ERRORS.append("USER_COOLDOWN 必须是非负有限数字")
    USER_COOLDOWN = 3.0


def validate_config() -> list[str]:
    """校验必填配置，返回错误列表（空列表表示通过）。同时记录关键告警。"""
    errors: list[str] = list(_PARSE_ERRORS)

    if API_ID <= 0:
        errors.append("API_ID 未设置或非法（应为 my.telegram.org 的数字 App api_id）")
    if not API_HASH:
        errors.append("API_HASH 未设置")
    if not BOT_TOKEN:
        errors.append("BOT_TOKEN 未设置（@BotFather 颁发的机器人令牌）")
    if not OWNER_ID:
        errors.append("OWNER_ID 未设置（至少一个管理员用户 ID）")

    if STRING and not LOG_GROUP:
        logger.warning(
            "已配置 STRING 但未设置 LOG_GROUP，Premium 大文件通道不可用，超过 2GB 的文件将无法上传"
        )
    # 与 tools/rotate_keys.py 的新密钥要求保持同一标准（≥32 字符）。
    if not MASTER_KEY or MASTER_KEY == _PUBLIC_MASTER_KEY or len(MASTER_KEY) < 32:
        errors.append(
            "MASTER_KEY 必须显式配置独立密钥（至少 32 字符），拒绝空值、短值或已公开的示例值"
        )
    if not SALT_KEY or SALT_KEY == _PUBLIC_SALT:
        errors.append("SALT_KEY 必须显式配置独立盐值，拒绝空值或已公开的示例值")
    return list(dict.fromkeys(errors))
