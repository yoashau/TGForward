"""相册部分成功的短期续传记录：仅复用确认成功成员，不缓存完整成功操作。"""

import asyncio
import hashlib
import json
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from tgforward.runtime import lifecycle

TTL = 3600
CAPACITY = 256
_records = OrderedDict()
_active = ContextVar("album_delivery", default=None)


@dataclass
class Record:
    sent: set = field(default_factory=set)
    timer: object = None
    message: object = None


def key_for(
    uploader, message, members, settings, destination, reply_to_message_id, *, source_private=False
):
    from tgforward.transfers.transfer import _album_captions, _media_caption
    from tgforward.utils.files import original_media_name, processed_name
    from tgforward.utils.media import custom_thumb_path

    me = getattr(uploader, "me", None)
    account = getattr(me, "id", None)
    identity = (
        ("telegram", account)
        if isinstance(account, int)
        else ("client", getattr(uploader, "name", type(uploader).__qualname__))
    )
    finals, originals = _album_captions(members, settings)
    names = []
    for member in members:
        name = original_media_name(member)
        names.append(
            processed_name(name, settings.delete_words, settings.replacements, settings.rename_tag)
            if name and source_private
            else name
        )
    thumb_digest = None
    if source_private:
        path = custom_thumb_path(settings.user_id)
        if path:
            with open(path, "rb") as source:
                thumb_digest = hashlib.sha256(source.read()).hexdigest()
    payload = [
        settings.user_id,
        lifecycle.current_generation(settings.user_id),
        identity,
        message.chat.id,
        str(message.media_group_id),
        [(m.id, str(getattr(m, "edit_date", None))) for m in members],
        [
            str(_media_caption(final if final is not None else original) or "")
            for final, original in zip(finals, originals, strict=True)
        ],
        names,
        thumb_digest,
        source_private,
        destination,
        reply_to_message_id,
    ]
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return f"{settings.user_id}:{lifecycle.current_generation(settings.user_id)}:{digest}"


def purge_user(user_id):
    prefix = f"{int(user_id)}:"
    for key in list(_records):
        if key.startswith(prefix):
            _drop(key)


def _drop(key):
    record = _records.pop(key, None)
    if record is not None and record.timer is not None:
        record.timer.cancel()
    return record


@contextmanager
def attempt(key):
    record = _drop(key) or Record()
    token = _active.set(record)
    try:
        yield record
    except BaseException:
        if record.sent:
            _records[key] = record
            record.timer = asyncio.get_running_loop().call_later(TTL, _drop, key)
            while len(_records) > CAPACITY:
                _drop(next(iter(_records)))
        raise
    finally:
        _active.reset(token)


def commit(keys):
    record = _active.get()
    if record is not None:
        record.sent.update(keys)


def sent():
    record = _active.get()
    return frozenset(record.sent) if record is not None else frozenset()


def remember_message(message=None):
    record = _active.get()
    if record is None:
        return message
    if record.message is None and message is not None:
        record.message = message
    return record.message


async def shutdown():
    for key in list(_records):
        _drop(key)
