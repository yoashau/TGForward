"""任务管理消息使用的评论动作与按钮。"""

from dataclasses import dataclass

from pyrogram.types import InlineKeyboardButton

from tgforward.ui.i18n import tr


@dataclass(frozen=True)
class CommentAction:
    chat_ref: str
    post_id: int
    comment_count: int | None
    has_comments: bool = True


def comment_button(action: CommentAction, owner_id: int) -> InlineKeyboardButton:
    count = action.comment_count
    label = tr("💬 提取评论（{0} 条）", count) if count else tr("💬 提取评论")
    return InlineKeyboardButton(
        label, callback_data=f"cmt:{action.chat_ref}:{action.post_id}:{owner_id}"
    )
