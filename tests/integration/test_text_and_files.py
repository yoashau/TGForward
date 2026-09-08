"""文件名、实体、链接、凭证迁移与真实 Pyrofork 参数的跨模块验证。"""

import asyncio
import base64
import inspect
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pyrogram import Client, enums, raw, utils
from pyrogram.types import MessageEntity

from tgforward.storage import crypto, users
from tgforward.storage.users import UserSettings
from tgforward.telegram import compat as pyrofork_compat
from tgforward.tools.rotate_keys import migrate, plan_document
from tgforward.transfers import transfer
from tgforward.utils import files, links
from tgforward.utils.text import (
    RichText,
    append_text,
    apply_text_rules,
    entity_kwargs,
    message_text,
    split_text,
    truncate_text,
    utf16_len,
)


def ent(kind, offset, length, **kwargs):
    return MessageEntity(
        type=getattr(enums.MessageEntityType, kind), offset=offset, length=length, **kwargs
    )


@pytest.mark.parametrize("stem", ["中" * 250, "😀" * 250, "a中😀" * 120, "x" * 500])
@pytest.mark.parametrize("ext", [".mp4", ".zip", ".mkv", ""])
def test_utf8_filename_preserves_extension(stem, ext, tmp_path):
    name = files.sanitize_filename(stem + ext)
    assert len(name.encode()) <= 250
    assert not ext or name.endswith(ext)
    (tmp_path / name).write_bytes(b"download starts")


def test_animation_original_filename_and_rules(tmp_path):
    msg = NS(animation=NS(file_name="old.gif"))
    assert transfer._has_original_name(msg)
    assert files.media_filename(msg) == "old.gif"
    source = tmp_path / "old.gif"
    source.write_bytes(b"GIF")
    target = files.apply_name_rules(str(source), [], {"old": "新" * 200}, "tag")
    assert Path(target).name.endswith(".gif")
    assert len(Path(target).name.encode()) <= 250


def test_concurrent_rename_never_overwrites(tmp_path):
    paths = [tmp_path / f"old{i}.txt" for i in range(8)]
    for i, path in enumerate(paths):
        path.write_text(str(i))
    rules = {f"old{i}": "same" for i in range(8)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        renamed = list(pool.map(lambda p: files.apply_name_rules(str(p), [], rules), paths))
    assert len(set(renamed)) == 8
    assert [Path(p).read_text() for p in renamed] == list(map(str, range(8)))


def test_rename_error_logs_warning(monkeypatch, tmp_path, caplog):
    path = tmp_path / "a.txt"
    path.write_bytes(b"a")
    monkeypatch.setattr(files.os, "link", lambda *a: (_ for _ in ()).throw(OSError("fixture")))
    assert files.apply_name_rules(str(path), [], {"a": "b"}) == str(path)
    assert "重命名失败" in caplog.text


@pytest.mark.parametrize(
    "text",
    [
        "    code  =  1  \n\tline\n",
        chr(96) * 3 + "python\n    print('x')\n" + chr(96) * 3,
        "  left   right  ",
        "\n\nuntouched\n\n",
    ],
)
def test_unmatched_text_rules_keep_all_whitespace(text):
    assert apply_text_rules(text, {"absent": "**literal**"}, ["never"]) == text


def test_rules_are_literal_and_entities_shift_in_utf16():
    original = RichText(
        "😀old link", [ent("BOLD", 2, 3), ent("TEXT_LINK", 6, 4, url="https://example.org/old")]
    )
    changed = apply_text_rules(original, {"old": "**新😀**"}, [])
    assert changed == "😀**新😀** link"
    assert [(e.offset, e.length) for e in changed.entities] == [(2, 7), (10, 4)]
    assert changed.entities[1].url == "https://example.org/old"
    assert [(e.offset, e.length) for e in original.entities] == [(2, 3), (6, 4)]


def test_delete_overlapping_entity_and_drop_stale_url():
    original = RichText("foo https://t.me/x", [ent("BOLD", 0, 3), ent("URL", 4, 14)])
    result = apply_text_rules(original, {"https": "oops"}, ["foo"])
    assert result == " oops://t.me/x"
    assert result.entities == []


def test_raw_message_never_reads_markdown_property():
    class Plain(str):
        @property
        def markdown(self):
            raise AssertionError("Markdown serialization must not occur")

    result = message_text(NS(text=Plain("  **literal**  "), entities=[ent("CODE", 2, 11)]))
    assert result == "  **literal**  "
    assert result.entities[0].length == 11


@pytest.mark.parametrize("kind", ["BOLD", "PRE", "TEXT_LINK"])
def test_entity_aware_split_roundtrip_utf16(kind):
    text = "😀" * 3000 + "\n    code\n\n" + "x" * 5000
    kwargs = {"url": "https://example.org"} if kind == "TEXT_LINK" else {}
    source = RichText(text, [ent(kind, 0, utf16_len(text), **kwargs)])
    chunks = split_text(source)
    assert "".join(chunks) == text
    assert len(chunks) >= 3
    assert all(0 < utf16_len(c) <= 4096 for c in chunks)
    for chunk in chunks:
        assert chunk.entities[0].offset == 0
        assert chunk.entities[0].length == utf16_len(chunk)
        if kwargs:
            assert chunk.entities[0].url == kwargs["url"]


def test_small_entity_moves_whole_to_next_chunk():
    source = RichText("aaaaaBBBBBBcc", [ent("BOLD", 5, 6)])
    chunks = split_text(source, 10)
    assert chunks == ["aaaaa", "BBBBBBcc"]
    assert chunks[0].entities == []
    assert chunks[1].entities[0].offset == 0
    assert chunks[1].entities[0].length == 6


def test_caption_truncation_respects_utf16_and_entities():
    cap = RichText("😀" * 600, [ent("ITALIC", 0, 1200)])
    result = truncate_text(cap, 1024)
    assert utf16_len(result) <= 1024 and result.endswith("…")
    assert result.entities[0].length == utf16_len(result) - 1


def test_caption_append_does_not_parse_user_markdown():
    cap = append_text(RichText("abc", [ent("BOLD", 0, 3)]), "**raw** [x](url)")
    assert cap == "abc\n\n**raw** [x](url)"
    assert cap.entities[0].length == 3


@pytest.mark.parametrize(
    "url",
    [
        "https://t.me/foo/12evil",
        "https://t.me/foo/12/extra",
        "https://t.me/foo/12/4/5",
        "https://t.me/foo/12#bad",
        "https://t.me/foo/12%20",
        "https://t.me/foo/0",
        "https://t.me/foo/12?comment=0",
        "https://t.me/foo/12?comment=1&comment=2",
        "https://t.me/foo/12?comment=abc",
        "https://t.me/foo-bar/12",
        "https://t.me/中文/12",
        "https://t.me:443/foo/12",
        "https://t.me/foo/2147483648",
        "https://evil.org@t.me/foo/12",
        "https://t.me/c/0/12",
        "https://t.me/foo/12\n",
        "https://t.me@evil.org/foo/12",
        "https://t.me/foo/0/12",
        "https://t.me/c/1/12tail",
    ],
)
def test_full_url_rejects_illegal_input(url):
    assert links.parse_link(url) is None


@pytest.mark.parametrize("suffix", ["evil", "/extra", "%20", "#bad", "?comment=0"])
def test_finder_does_not_extract_valid_prefix(suffix):
    assert links.find_links("https://t.me/foo/12" + suffix) == []


def test_finder_contract_and_punctuation():
    found = links.find_links(
        "(https://t.me/foo/12)。 https://t.me/foo-bar/13 https://t.me/c/123/14?single"
    )
    assert found == ["https://t.me/foo/12", "https://t.me/c/123/14?single"]
    assert all(links.parse_link(url) for url in found)


def legacy_encrypt(plain, key):
    nonce = os.urandom(12)
    cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    data = cipher.update(plain.encode()) + cipher.finalize()
    return base64.b64encode(nonce + cipher.tag + data).decode()


def test_version_prefix_authenticated_and_legacy_compatible():
    key = crypto._derive_key("old-master", "old-salt")
    assert crypto.decrypt(legacy_encrypt("session", key), key=key) == "session"
    token = crypto.encrypt("session", key=key)
    assert token.startswith("v1:")
    assert crypto.decrypt(token, key=key) == "session"
    with pytest.raises(InvalidTag):
        crypto.decrypt(token[3:], key=key)
    with pytest.raises(ValueError):
        crypto.decrypt(token.replace("v1:", "v2:"), key=key)


def test_migrate_all_credential_fields_and_idempotent():
    old_key, new_key = b"o" * 16, b"n" * 16
    doc = {
        "_id": 1,
        "session_string": legacy_encrypt("s", old_key),
        "bot_token": "123:old",
        "pending_logouts": {
            "abc": {"session_string": legacy_encrypt("pending", old_key), "error": "unchanged"}
        },
    }
    before, update = plan_document(doc, old_key, new_key)
    assert set(update) == {"session_string", "bot_token", "pending_logouts.abc.session_string"}
    assert [crypto.decrypt(value, key=new_key) for value in update.values()] == [
        "s",
        "123:old",
        "pending",
    ]
    assert doc["session_string"] == before["session_string"]
    doc["session_string"], doc["bot_token"] = update["session_string"], update["bot_token"]
    doc["pending_logouts"]["abc"]["session_string"] = update["pending_logouts.abc.session_string"]
    assert plan_document(doc, old_key, new_key) == ({}, {})


@pytest.mark.parametrize("apply", [False, True])
def test_migrate_dry_run_and_atomic_update(sqlite_store, apply):
    stored = legacy_encrypt("s", b"o" * 16)
    asyncio.run(sqlite_store.mutate_user(1, lambda doc: doc.update(session_string=stored)))
    result = asyncio.run(migrate(sqlite_store, b"o" * 16, b"n" * 16, apply=apply))
    assert result["fields"] == 1 and result["updated"] == int(apply)
    assert result["conflicts"] == 0
    value = asyncio.run(sqlite_store.get_user(1))["session_string"]
    if apply:
        assert crypto.decrypt(value, key=b"n" * 16) == "s"
        assert asyncio.run(migrate(sqlite_store, b"o" * 16, b"n" * 16))["fields"] == 0
    else:
        assert value == stored


def test_migrate_bad_key_never_writes_document(sqlite_store):
    stored = legacy_encrypt("s", b"x" * 16)
    asyncio.run(sqlite_store.mutate_user(1, lambda doc: doc.update(session_string=stored)))
    counts = asyncio.run(migrate(sqlite_store, b"o" * 16, b"n" * 16, apply=True))
    assert counts["errors"] == 1 and counts["updated"] == 0
    assert asyncio.run(sqlite_store.get_user(1))["session_string"] == stored


def test_versioned_bot_ciphertext_never_falls_back_to_plaintext(monkeypatch):
    token = crypto.encrypt("123:secret", key=b"x" * 16)
    monkeypatch.setattr(users, "get_user", AsyncMock(return_value={"bot_token": token}))
    assert asyncio.run(users.get_helper_token(1)) is None


def status():
    return NS(id=9, chat=NS(id=1), edit=AsyncMock(), delete=AsyncMock())


def media_message(kind="video", caption=""):
    fields = {
        k: None
        for k in (
            "video",
            "audio",
            "document",
            "animation",
            "photo",
            "voice",
            "sticker",
            "video_note",
        )
    }
    fields[kind] = NS(file_name="original.mp4", file_size=10)
    return NS(
        **fields,
        text=None,
        caption=caption,
        caption_entities=[],
        media=True,
        id=5,
        chat=NS(id=-1001, username="source"),
        media_group_id=None,
    )


@pytest.mark.parametrize("premium", [False, True])
def test_video_unknown_metadata_omitted_but_caption_entities_sent(monkeypatch, premium):
    send = AsyncMock(return_value=NS(id=10))
    client = NS(send_video=send, copy_message=AsyncMock(return_value=NS(id=11)))
    message = media_message(caption="caption")
    cap = RichText("caption", [ent("BOLD", 0, 7)])
    if premium:
        monkeypatch.setattr(transfer.clients_registry, "premium", client)
        monkeypatch.setattr(transfer.clients_registry, "premium_started", True)
        monkeypatch.setattr(transfer, "LOG_GROUP", -1002)
    func = transfer._upload_large if premium else transfer._upload_regular
    asyncio.run(
        func(
            client,
            message,
            "original.mp4",
            cap,
            None,
            None,
            -1003,
            transfer.Destination(-1003, 7, 8),
            status(),
            None,
        )
    )
    kwargs = send.call_args.kwargs
    inspect.signature(Client.send_video).bind(client, *send.call_args.args, **kwargs)
    assert not {"width", "height", "duration"} & kwargs.keys()
    assert kwargs["caption"] == "caption"
    assert kwargs["caption_entities"][0].length == 7
    assert kwargs["parse_mode"] == enums.ParseMode.DISABLED


def test_text_transfer_preserves_all_chunks_and_entities_and_topic():
    text = "😀" * 2400 + "\n    tail  "
    original = RichText(text, [ent("PRE", 0, utf16_len(text))])
    send = AsyncMock(return_value=status())
    msg = NS(
        text=original,
        entities=original.entities,
        media=None,
        id=4,
        chat=NS(id=2),
        media_group_id=None,
    )
    asyncio.run(
        transfer.transfer_message(
            NS(send_message=send),
            NS(),
            msg,
            UserSettings(1, chat_id="-1001/9"),
            1,
            source_private=False,
        )
    )
    assert "".join(call.args[1] for call in send.call_args_list) == str(original)
    for call in send.call_args_list:
        inspect.signature(Client.send_message).bind(NS(), *call.args, **call.kwargs)
        assert call.kwargs["message_thread_id"] == 9
        assert call.kwargs["parse_mode"] == enums.ParseMode.DISABLED
        assert all(e.offset + e.length <= utf16_len(call.args[1]) for e in call.kwargs["entities"])


def test_real_pyrofork_literal_parse_keeps_whitespace():
    pyrofork_compat.install()
    client = NS(parser=NS(parse=AsyncMock(side_effect=AssertionError("must bypass parser"))))
    text = "  **literal**  \n"
    result = asyncio.run(
        utils.parse_text_entities(
            client, text, **{"parse_mode": enums.ParseMode.DISABLED, "entities": []}
        )
    )
    assert result == {"message": text, "entities": None}


@pytest.mark.parametrize("changed", [False, True])
def test_real_copy_album_preserves_entities_without_markdown(monkeypatch, changed):
    from pyrogram.methods.messages.copy_media_group import CopyMediaGroup

    pyrofork_compat.install()
    source = [
        NS(
            photo=NS(file_id="photo"),
            caption="  **raw**  ",
            caption_entities=[ent("BOLD", 2, 7)],
            has_media_spoiler=False,
        )
    ]
    c = NS(
        parser=NS(parse=AsyncMock(side_effect=AssertionError("no Markdown"))),
        rnd_id=lambda: 1,
        get_media_group=AsyncMock(return_value=source),
        resolve_peer=AsyncMock(return_value=raw.types.InputPeerSelf()),
        invoke=AsyncMock(return_value=NS(updates=[], users=[], chats=[])),
    )
    monkeypatch.setattr(
        utils, "get_input_media_from_file_id", lambda **kw: raw.types.InputMediaEmpty()
    )
    monkeypatch.setattr(utils, "get_reply_to", AsyncMock(return_value=None))
    captions = [RichText("😀new", [ent("ITALIC", 2, 3)])] if changed else None
    asyncio.run(
        CopyMediaGroup.copy_media_group(
            c, 1, 2, 3, captions=captions, parse_mode=enums.ParseMode.DISABLED
        )
    )
    sent = c.invoke.call_args.args[0].multi_media[0]
    assert sent.message == ("😀new" if changed else "  **raw**  ")
    assert sent.entities[0].offset == 2
    assert sent.entities[0].length == (3 if changed else 7)


def test_public_copy_modified_caption_retains_entities_and_literal_replacement():
    msg = media_message(caption="😀old")
    msg.caption_entities = [ent("BOLD", 2, 3)]
    copier = AsyncMock(return_value=NS(id=90))
    asyncio.run(
        transfer.transfer_message(
            NS(copy_message=copier),
            NS(),
            msg,
            UserSettings(1, replacements={"old": "**new**"}),
            1,
            source_private=False,
        )
    )
    kwargs = copier.call_args.kwargs
    assert kwargs["caption"] == "😀**new**"
    assert kwargs["caption_entities"][0].length == 7
    assert kwargs["parse_mode"] == enums.ParseMode.DISABLED


def test_entity_kwargs_does_not_interpret_markup():
    assert entity_kwargs(RichText("**raw**")) == {
        "parse_mode": enums.ParseMode.DISABLED,
        "entities": [],
    }


def test_replacement_crossing_adjacent_entities_does_not_create_overlaps():
    source = RichText("abcDEFghiJ", [ent("BOLD", 0, 5), ent("ITALIC", 5, 5)])
    result = apply_text_rules(source, {"DEFg": "XX"}, [])
    assert result == "abcXXhiJ"
    assert [(e.offset, e.length) for e in result.entities] == [(0, 3), (5, 3)]


def test_noop_replacement_preserves_url_entity():
    source = RichText("https://x.org", [ent("URL", 0, 13)])
    assert apply_text_rules(source, {"https": "https"}, []).entities[0].length == 13


def test_wire_entity_trims_span_not_text():
    source = RichText("  code  \n", [ent("PRE", 2, 7)])
    kwargs = entity_kwargs(source)
    assert kwargs["entities"][0].offset == 2
    assert kwargs["entities"][0].length == 4
    assert source == "  code  \n" and source.entities[0].length == 7
