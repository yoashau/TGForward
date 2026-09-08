"""处理器共享的小工具。"""

from tgforward.storage.users import is_whitelisted

DENY_TEXT = "⚠️ 你没有使用权限，请联系管理员。"


async def ensure_whitelisted(message) -> bool:
    """白名单校验，不通过时直接回复拒绝消息。"""
    if not await is_whitelisted(message.from_user.id):
        await message.reply(DENY_TEXT)
        return False
    return True
