"""Pyrofork 相册适配：逐项保留文案实体，明确区分空文案和沿用文案。"""

from pyrogram import raw, utils


async def copy_media_group(
    self,
    chat_id,
    from_chat_id,
    message_id,
    captions=None,
    has_spoilers=None,
    disable_notification=None,
    message_thread_id=None,
    send_as=None,
    reply_to_message_id=None,
    reply_to_chat_id=None,
    reply_to_story_id=None,
    quote_text=None,
    parse_mode=None,
    quote_entities=None,
    quote_offset=None,
    schedule_date=None,
    invert_media=None,
    protect_content=None,
    allow_paid_broadcast=None,
    message_effect_id=None,
):
    """复制现有媒体引用；签名与 Pyrofork 2.3.69 的 copy_media_group 一致。"""
    messages = await self.get_media_group(from_chat_id, message_id)
    items = []
    for index, message in enumerate(messages):
        media = next(
            (
                getattr(message, kind, None)
                for kind in ("photo", "audio", "document", "video")
                if getattr(message, kind, None)
            ),
            None,
        )
        if media is None:
            raise ValueError("Message with this type can't be copied.")
        if isinstance(has_spoilers, list) and index < len(has_spoilers):
            spoiler = has_spoilers[index]
        elif isinstance(has_spoilers, bool):
            spoiler = has_spoilers
        else:
            spoiler = message.has_media_spoiler
        items.append(
            raw.types.InputSingleMedia(
                media=utils.get_input_media_from_file_id(
                    file_id=media.file_id, has_spoiler=spoiler
                ),
                random_id=self.rnd_id(),
                **await parse_caption(self, captions, index, message, parse_mode),
            )
        )
    reply_to = await utils.get_reply_to(
        client=self,
        chat_id=chat_id,
        reply_to_message_id=reply_to_message_id,
        message_thread_id=message_thread_id,
        reply_to_chat_id=reply_to_chat_id,
        reply_to_story_id=reply_to_story_id,
        quote_text=quote_text,
        quote_entities=quote_entities,
        parse_mode=parse_mode,
        quote_offset=quote_offset,
    )
    result = await self.invoke(
        raw.functions.messages.SendMultiMedia(
            peer=await self.resolve_peer(chat_id),
            multi_media=items,
            silent=disable_notification or None,
            reply_to=reply_to,
            send_as=await self.resolve_peer(send_as) if send_as else None,
            schedule_date=utils.datetime_to_timestamp(schedule_date),
            noforwards=protect_content,
            invert_media=invert_media,
            allow_paid_floodskip=allow_paid_broadcast,
            effect=message_effect_id,
        ),
        sleep_threshold=60,
    )
    updates = (
        raw.types.UpdateNewMessage,
        raw.types.UpdateNewChannelMessage,
        raw.types.UpdateNewScheduledMessage,
    )
    return await utils.parse_messages(
        self,
        raw.types.messages.Messages(
            messages=[update.message for update in result.updates if isinstance(update, updates)],
            users=result.users,
            chats=result.chats,
        ),
    )


async def parse_caption(client, captions, index, message, parse_mode):
    from pyrogram import enums

    from tgforward.utils.text import RichText, message_text, truncate_text, wire_entities

    if isinstance(captions, list) and index < len(captions) and captions[index] is not None:
        caption = captions[index]
    elif isinstance(captions, str):
        caption = captions if index == 0 else RichText("")
    else:
        caption = message_text(message, "caption")
    if isinstance(caption, RichText):
        caption = truncate_text(caption, 1024)
        entities = []
        for entity in wire_entities(caption):
            entity._client = client
            entities.append(await entity.write())
        # Pyrofork 2.3.69 的 TL writer 对 [] 不设置 entities flag，
        # 却仍写入空 Vector（8 字节），导致相册成员错位/请求尾部残留。
        # 与原生 parser 一致：没有实体必须传 None，而不是 []。
        return {"message": str(caption), "entities": entities or None}
    if parse_mode == enums.ParseMode.DISABLED:
        return {"message": str(caption), "entities": None}
    # 公共 API 的显式字符串调用保持其原有解析契约。
    if parse_mode is None:
        return await client.parser.parse(caption)
    return await client.parser.parse(caption, parse_mode)
