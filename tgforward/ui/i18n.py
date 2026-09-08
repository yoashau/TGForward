"""Request-local interface language; source content is never translated."""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_language = ContextVar("ui_language", default="zh")
CHINESE = json.loads((Path(__file__).parent / "locales" / "zh.json").read_text(encoding="utf-8"))
ENGLISH = json.loads((Path(__file__).parent / "locales" / "en.json").read_text(encoding="utf-8"))


def current_language():
    return _language.get()


def set_language(language):
    if language not in ("zh", "en"):
        raise ValueError("unsupported interface language")
    _language.set(language)


def normalize_language(code):
    return "zh" if not code or str(code).lower().startswith("zh") else "en"


@contextmanager
def language_context(language):
    token = _language.set(language if language in ("zh", "en") else "zh")
    try:
        yield
    finally:
        _language.reset(token)


def tr(template, *values):
    source = CHINESE.get(template, template)
    translated = ENGLISH.get(template, source) if current_language() == "en" else source
    return translated.format(*values) if values else translated


async def user_language(user):
    from tgforward.storage.users import get_user

    uid = getattr(user, "id", None)
    doc = (await get_user(uid) or {}) if uid is not None else {}
    selected = doc.get("ui_language")
    return (
        selected
        if selected in ("zh", "en")
        else normalize_language(getattr(user, "language_code", None))
    )
