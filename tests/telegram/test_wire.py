"""Exercise real Pyrofork TL serialization, not just objects handed to invoke.

Only reads/network responses are stubbed. File-id decoding, text parsing,
reply/keyboard construction, RPC construction and TL read/write stay real.
These tests check wire structure; they do not claim Telegram accepted a send.
"""

import asyncio
import inspect
import runpy
from datetime import UTC, datetime
from functools import partial
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pyrogram import Client, enums, raw, types, utils
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.parser import Parser
from pyrogram.raw.core import TLObject

from tgforward.telegram import compat as pyrofork_compat
from tgforward.telegram.albums import parse_caption
from tgforward.utils.text import RichText


def test_album_adapter_matches_installed_api_without_source_rewriting(monkeypatch):
    import pyrogram

    from tgforward.telegram.albums import copy_media_group

    source = Path(pyrogram.__file__).parent / "methods/messages/copy_media_group.py"
    native = runpy.run_path(str(source))["CopyMediaGroup"].copy_media_group

    def parameters(function):
        return [
            (p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()
        ]

    assert parameters(copy_media_group) == parameters(native)
    monkeypatch.setattr(inspect, "getsource", lambda _: pytest.fail("runtime source rewriting"))
    pyrofork_compat.install()
    assert Client.copy_media_group is copy_media_group


def test_album_quote_and_delivery_options_serialize():
    source = [
        NS(
            photo=NS(file_id=photo_id(i)),
            caption=None,
            caption_entities=None,
            has_media_spoiler=False,
        )
        for i in (1, 2)
    ]
    client, requests = wire_client(source)
    asyncio.run(
        Client.copy_media_group(
            client,
            1,
            "source",
            1,
            has_spoilers=[True, False],
            disable_notification=True,
            protect_content=True,
            invert_media=True,
            allow_paid_broadcast=True,
            message_effect_id=123,
            schedule_date=datetime(2030, 1, 1, tzinfo=UTC),
            send_as=2,
            reply_to_message_id=77,
            message_thread_id=9,
            quote_text="quoted",
            quote_offset=2,
            quote_entities=[
                types.MessageEntity(type=enums.MessageEntityType.BOLD, offset=0, length=6)
            ],
        )
    )
    request = requests[0]
    assert request.reply_to.quote_text == "quoted"
    assert request.reply_to.quote_offset == 2
    assert isinstance(request.reply_to.quote_entities[0], raw.types.MessageEntityBold)
    assert [bool(item.media.spoiler) for item in request.multi_media] == [True, False]
    assert request.silent and request.noforwards and request.invert_media
    assert request.allow_paid_floodskip and request.effect == 123
    assert request.schedule_date == 1893456000
    assert request.send_as.user_id == 1


def run_call(make_call):
    # Invoke inside the loop so Pyrofork's sync wrapper returns an awaitable.
    async def run():
        return await make_call()

    return asyncio.run(run())


def decode_exact(value):
    stream = BytesIO(value.write())
    decoded = TLObject.read(stream)
    assert type(decoded) is type(value)
    assert stream.read() == b"", "TL flags omitted a field that write() serialized"
    return decoded


def photo_id(index=1):
    return FileId(
        file_type=FileType.PHOTO,
        dc_id=2,
        media_id=index,
        access_hash=123,
        file_reference=b"fixture",
        volume_id=1,
        local_id=1,
        thumbnail_source=ThumbnailSource.THUMBNAIL,
        thumbnail_file_type=FileType.PHOTO,
        thumbnail_size="y",
    ).encode()


def wire_client(source=()):
    pyrofork_compat.install()
    requests = []

    async def invoke(request, **kwargs):
        if isinstance(request, raw.functions.messages.SendMultiMedia):
            # A permissive Vector reader can consume stray vectors as items;
            # check each item independently as well as the complete request.
            for item in request.multi_media:
                decode_exact(item)
        decoded = decode_exact(request)
        if isinstance(request, raw.functions.messages.SendMultiMedia):
            assert len(decoded.multi_media) == len(request.multi_media)
            assert all(isinstance(item, raw.types.InputSingleMedia) for item in decoded.multi_media)
        requests.append(decoded)
        return NS(updates=[], users=[], chats=[])

    client = NS(
        parser=Parser(None),
        rnd_id=lambda: 123,
        get_media_group=AsyncMock(return_value=list(source)),
        resolve_peer=AsyncMock(return_value=raw.types.InputPeerUser(user_id=1, access_hash=2)),
        invoke=invoke,
    )
    return client, requests


@pytest.mark.parametrize(
    "case",
    [
        "unchanged",
        "empty",
        "plain",
        "rich_empty",
        "rich_entity",
        "invalid_entity",
        "whitespace_entity",
        "markdown",
        "html",
    ],
)
def test_album_caption_wire_contract(case):
    client, _ = wire_client()
    entity = types.MessageEntity(type=enums.MessageEntityType.BOLD, offset=0, length=3)
    source = NS(caption="old", caption_entities=None)
    caption, mode = {
        "unchanged": (None, None),
        "empty": ([""], enums.ParseMode.DISABLED),
        "plain": (["  **raw**  "], enums.ParseMode.DISABLED),
        "rich_empty": ([RichText("")], None),
        "rich_entity": ([RichText("new", [entity])], None),
        "invalid_entity": ([RichText("x", [entity])], None),
        "whitespace_entity": ([RichText("   ", [entity])], None),
        "markdown": (["**new**"], enums.ParseMode.MARKDOWN),
        "html": (["<b>new</b>"], enums.ParseMode.HTML),
    }[case]
    parsed = asyncio.run(parse_caption(client, caption, 0, source, mode))
    item = raw.types.InputSingleMedia(
        media=raw.types.InputMediaEmpty(),
        random_id=1,
        **parsed,
    )
    decoded = decode_exact(item)
    assert decoded.message == parsed["message"]
    assert len(decoded.entities) == len(parsed["entities"] or [])
    if not decoded.entities:
        assert parsed["entities"] is None


@pytest.mark.parametrize("captions", [None, ["", None], "", [RichText("  **raw**  ")]])
@pytest.mark.parametrize("with_reply", [False, True])
def test_real_eight_item_copy_serializes(captions, with_reply):
    source = [
        NS(
            photo=NS(file_id=photo_id(i)),
            caption="old" if i == 1 else None,
            caption_entities=None,
            has_media_spoiler=False,
        )
        for i in range(1, 9)
    ]
    client, requests = wire_client(source)
    kwargs = {"reply_to_message_id": 77, "message_thread_id": 9} if with_reply else {}
    asyncio.run(
        Client.copy_media_group(
            client,
            1,
            "Cos_zg",
            9691,
            captions=captions,
            parse_mode=enums.ParseMode.DISABLED,
            **kwargs,
        )
    )
    assert len(requests) == 1
    request = requests[0]
    assert len(request.multi_media) == 8
    assert [item.media.id.id for item in request.multi_media] == list(range(1, 9))
    expected = "old" if captions is None else str(captions[0]) if isinstance(captions, list) else ""
    assert [item.message for item in request.multi_media] == [expected] + [""] * 7
    if with_reply:
        assert request.reply_to.reply_to_msg_id == 77
        assert request.reply_to.top_msg_id == 9


@pytest.mark.parametrize("method", ["send_message", "edit_message_text", "send_cached_media"])
@pytest.mark.parametrize("formatted", [False, True])
def test_real_text_menu_and_single_media_serialize(method, formatted):
    client, requests = wire_client()
    text = "  **literal** 😀  \n"
    entities = [types.MessageEntity(type=enums.MessageEntityType.BOLD, offset=2, length=7)]
    markup = types.InlineKeyboardMarkup(
        [
            [types.InlineKeyboardButton("返回", callback_data="menu:home")],
        ]
    )
    kwargs = {"parse_mode": enums.ParseMode.DISABLED, "reply_markup": markup}
    if method == "send_cached_media":
        call = partial(
            Client.send_cached_media,
            client,
            1,
            photo_id(),
            caption=text,
            caption_entities=entities if formatted else [],
            reply_to_message_id=77,
            message_thread_id=9,
            **kwargs,
        )
    elif method == "send_message":
        call = partial(
            Client.send_message,
            client,
            1,
            text,
            entities=entities if formatted else [],
            reply_to_message_id=77,
            message_thread_id=9,
            **kwargs,
        )
    else:
        call = partial(
            Client.edit_message_text,
            client,
            1,
            5,
            text,
            entities=entities if formatted else [],
            **kwargs,
        )
    run_call(call)
    assert len(requests) == 1
    assert requests[0].message == text
    assert len(requests[0].entities) == int(formatted)
    assert requests[0].reply_markup.rows[0].buttons[0].data == b"menu:home"


def test_real_send_media_group_serializes():
    client, requests = wire_client()
    media = [
        types.InputMediaPhoto(
            photo_id(i),
            caption="  **literal**  " if i == 1 else "",
            parse_mode=enums.ParseMode.DISABLED,
        )
        for i in range(1, 9)
    ]
    run_call(partial(Client.send_media_group, client, 1, media))
    assert [item.message for item in requests[0].multi_media] == ["  **literal**  "] + [""] * 7


def test_plain_caption_matches_native_parser_bytes():
    client, _ = wire_client()
    source = NS(caption="plain", caption_entities=None)
    native = asyncio.run(client.parser.parse(source.caption))
    compat = asyncio.run(parse_caption(client, None, 0, source, None))
    kwargs = {"media": raw.types.InputMediaEmpty(), "random_id": 1}
    assert raw.types.InputSingleMedia(**kwargs, **compat).write() == (
        raw.types.InputSingleMedia(**kwargs, **native).write()
    )


def test_literal_parser_without_entities_uses_native_absence_contract():
    wire_client()
    parsed = asyncio.run(
        utils.parse_text_entities(
            None,
            "  literal  ",
            enums.ParseMode.DISABLED,
            [],
        )
    )
    assert parsed == {"message": "  literal  ", "entities": None}


def test_comment_state_updates_serialize_text_and_keyboard_together():
    from tgforward.comments.actions import LABELS, CommentButton
    from tgforward.runtime.tasks import Task
    from tgforward.transfers.progress import result_keyboard
    from tgforward.ui.keyboards import CommentAction

    client, requests = wire_client()
    client.edit_message_text = partial(Client.edit_message_text, client)
    task = Task(42, "comments", 1)

    async def change():
        for state in LABELS:
            button = CommentButton(
                client,
                NS(id=77, chat=NS(id=1), text="原帖提取成功"),
                result_keyboard(task, "success", CommentAction("source", 7, 8)),
                task=task,
            )
            await button.finish(outcome=state)

    asyncio.run(change())
    assert len(requests) == len(LABELS)
    for request, label in zip(requests, LABELS.values(), strict=True):
        assert request.id == 77
        assert "原帖提取成功" in request.message and "💬 评论提取" in request.message
        assert request.media is None
        actual = request.reply_markup.rows[0].buttons[1]
        assert actual.text == label
        expected = (
            f"flow:cancel:42:{task.token}"
            if label == LABELS["running"]
            else "flow:result:42:comments_success"
            if label == LABELS["success"]
            else "cmt:source:7:42"
        )
        assert actual.data == expected.encode()
