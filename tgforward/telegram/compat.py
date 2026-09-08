"""Pyrofork 2.3.69 定向兼容：媒体会话、topics 缺省值和相册空 caption。"""

from functools import wraps
from inspect import Parameter, signature

import pyrogram
from pyrogram import raw


def install():
    if pyrogram.__version__ == "2.3.69":
        from tgforward.telegram.media_session import get_file

        pyrogram.Client.get_file = get_file
        _install_album_captions()
        _install_literal_text()
    cls = raw.types.messages.Messages
    original = cls.__init__
    if pyrogram.__version__ != "2.3.69" or getattr(original, "_topics_compat", False):
        return
    parameter = signature(original).parameters.get("topics")
    if parameter is None or parameter.default is not Parameter.empty:
        return

    @wraps(original)
    def init(self, *, messages, chats, users, topics=None):
        original(
            self,
            messages=messages,
            chats=chats,
            users=users,
            topics=[] if topics is None else topics,
        )

    init._topics_compat = True
    cls.__init__ = init


def _install_album_captions():
    from pyrogram.methods.messages.copy_media_group import CopyMediaGroup

    from tgforward.telegram.albums import copy_media_group

    CopyMediaGroup.copy_media_group = copy_media_group
    pyrogram.Client.copy_media_group = copy_media_group


def _install_literal_text():
    """Pyrofork 即使 DISABLED 也 strip；仅该模式绕过 parser，保留原始空白。"""
    from pyrogram import enums, utils

    original = utils.parse_text_entities
    if getattr(original, "_literal_text_compat", False):
        return

    @wraps(original)
    async def parse(client, text, parse_mode, entities):
        if parse_mode == enums.ParseMode.DISABLED and not entities:
            # SendMessage/EditMessage/SendMedia 及引用实体共享同一 TL 契约。
            return {"message": str(text) if text is not None else "", "entities": None}
        return await original(client, text, parse_mode, entities)

    parse._literal_text_compat = True
    utils.parse_text_entities = parse
