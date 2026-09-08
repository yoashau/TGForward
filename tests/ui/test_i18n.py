"""Language isolation, complete interface templates, and unchanged user content."""

import ast
import asyncio
import re
from pathlib import Path
from string import Formatter
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from tgforward.handlers import menu, settings, start
from tgforward.storage import users
from tgforward.ui import panel
from tgforward.ui.i18n import (
    CHINESE,
    ENGLISH,
    current_language,
    language_context,
    tr,
    user_language,
)

HAN = re.compile(r"[\u4e00-\u9fff]")


def fields(text):
    return {name for _, name, _, _ in Formatter().parse(text) if name is not None}


def test_catalog_preserves_template_arguments():
    for source, english in ENGLISH.items():
        assert fields(CHINESE.get(source, source)) == fields(english), source
        assert english


def test_all_literal_translation_calls_have_english_entries():
    for path in Path("tgforward").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tr"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                assert node.args[0].value in ENGLISH, (path, node.lineno, node.args[0].value)


@pytest.mark.parametrize("name", ["home", "account", "telegram", "helper", "admin", "language"])
def test_all_navigation_pages_follow_selected_language(name):
    with language_context("en"):
        text, markup = menu.page(name, 1)
        assert not HAN.search(text)
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data != "nav:language:zh":
                    assert not HAN.search(button.text)
    with language_context("zh"):
        text, _ = menu.page(name, 1)
        assert HAN.search(text)


@pytest.mark.parametrize("category", ["home", "transfer", "content"])
def test_settings_keyboard_has_english_labels_and_identical_routes(category):
    def collect(language):
        with language_context(language):
            return [b for row in settings.settings_keyboard(category).inline_keyboard for b in row]

    english, chinese = collect("en"), collect("zh")
    assert [b.callback_data for b in english] == [b.callback_data for b in chinese]
    assert all(not HAN.search(b.text) for b in english)


@pytest.mark.parametrize("key", list(settings.PROMPTS))
def test_form_prompts_are_localized(key):
    with language_context("en"):
        assert not HAN.search(tr(settings.PROMPTS[key]))
    with language_context("zh"):
        assert HAN.search(tr(settings.PROMPTS[key]))


def test_dynamic_user_text_is_not_translated_or_formatted_twice():
    value = "主菜单 {0} **我的频道** 🔐"
    with language_context("en"):
        assert tr("✅ 文件名标签已设置为：`{0}`", value) == f"✅ Filename tag set to: `{value}`"
        assert tr("✅ 替换规则已保存：`{0}` → `{1}`", value, value).count(value) == 2


def test_concurrent_requests_and_background_tasks_keep_language():
    async def run():
        ready = asyncio.Event()

        async def background():
            await ready.wait()
            return tr("主菜单")

        with language_context("en"):
            english = asyncio.create_task(background())
        with language_context("zh"):
            chinese = asyncio.create_task(background())
            ready.set()
            assert await english == "Main menu"
            assert await chinese == "主菜单"
        assert current_language() == "zh"

    asyncio.run(run())


def test_preference_survives_panel_recreation_and_rejects_missing_users(sqlite_store):
    async def run():
        await sqlite_store.mutate_user(9876, lambda doc: doc.update(is_whitelisted=True))
        assert await users.set_ui_language(9876, "en")
        panel._panels.clear()
        assert await user_language(NS(id=9876, language_code="zh")) == "en"
        assert await users.set_ui_language(9876, "zh")
        assert await user_language(NS(id=9876, language_code="en")) == "zh"
        await sqlite_store.delete_user(9876)
        assert not await users.set_ui_language(9876, "en")
        assert await users.get_user(9876) is None
        with pytest.raises(ValueError):
            await users.set_ui_language(9876, "invalid")

    asyncio.run(run())


def test_revoked_user_language_cannot_recreate_data(sqlite_store, monkeypatch):
    from tgforward.runtime import lifecycle

    monkeypatch.setattr(lifecycle, "revoked", {9876})
    assert asyncio.run(users.set_ui_language(9876, "en")) is False
    assert asyncio.run(users.get_user(9876)) is None


def test_command_descriptions_follow_user_language(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(start.bot, "set_bot_commands", send)
    asyncio.run(start.configure_user_commands(9876, "en"))
    assert [c.description for c in send.call_args.args[0]] == [
        "Main menu",
        "Extraction settings",
        "Account & history",
    ]
    assert send.call_args.kwargs["scope"].chat_id == 9876
    asyncio.run(start.configure_user_commands(9876, "zh"))
    assert send.call_args.args[0][0].description == "主菜单"


def test_english_result_and_cancel_buttons():
    from tgforward.runtime.tasks import Task
    from tgforward.transfers.progress import result_keyboard, stop_keyboard
    from tgforward.transfers.results import CommentResult

    with language_context("en"):
        task = Task(9876, "single", 1)
        for outcome in ("success", "failed", "incomplete", "uncertain", "stopped"):
            markup = result_keyboard(task, outcome)
            assert all(not HAN.search(b.text) for row in markup.inline_keyboard for b in row)
        assert not HAN.search(stop_keyboard(task).inline_keyboard[0][0].text)
        assert not HAN.search(task.media_progress())
        assert not HAN.search(CommentResult().summary())


@pytest.mark.parametrize("language", ["zh", "en"])
def test_transfer_preserves_source_text_and_delivery_outcome(language, monkeypatch):
    from tests.transfers.test_routing import _text_message
    from tgforward.runtime.tasks import Task
    from tgforward.transfers import transfer

    monkeypatch.setattr(transfer, "_MIN_SEND_INTERVAL", 0)
    monkeypatch.setattr(transfer, "_last_send_at", 0)
    monkeypatch.setattr(transfer, "_send_lock", asyncio.Lock())
    content = "主菜单 {0} 用户的内容 English 📩"
    send = AsyncMock(return_value=NS(id=123))
    with language_context(language):
        result = asyncio.run(
            transfer.transfer_message(
                NS(send_message=send),
                NS(),
                _text_message(content),
                users.UserSettings(9876),
                "9876",
                source_private=False,
                task=Task(9876, "single", 1),
            )
        )
    assert send.call_args.args[1] == content
    assert result.outcome == "success"
    assert result.delivery.confirmed_delivered == {"text:0"}
    assert bool(HAN.search(result.summary)) == (language == "zh")


@pytest.mark.parametrize("language", ["zh", "en"])
def test_cooperative_cancel_commits_only_completed_text_parts(language, monkeypatch):
    from tests.transfers.test_routing import _text_message
    from tgforward.runtime.tasks import CancelReason, Task, TaskCancelled
    from tgforward.transfers import transfer

    monkeypatch.setattr(transfer, "_MIN_SEND_INTERVAL", 0)
    monkeypatch.setattr(transfer, "_last_send_at", 0)
    monkeypatch.setattr(transfer, "_send_lock", asyncio.Lock())
    task = Task(9876, "single", 1)

    async def send(*args, **kwargs):
        task.set_cancel_reason(CancelReason.USER)
        return NS(id=1)

    with language_context(language), pytest.raises(TaskCancelled):
        asyncio.run(
            transfer.transfer_message(
                NS(send_message=send),
                NS(),
                _text_message("文" * 9000),
                users.UserSettings(9876),
                "9876",
                source_private=False,
                task=task,
            )
        )
    result = task.units[0].message_results[0]
    assert result.delivery.confirmed_delivered == {"text:0"}
    assert result.delivery.not_attempted == {"text:1", "text:2"}
    assert result.outcome == "incomplete"


def test_language_lookup_accepts_missing_sender(monkeypatch):
    get = AsyncMock()
    monkeypatch.setattr(users, "get_user", get)
    assert asyncio.run(user_language(None)) == "zh"
    get.assert_not_awaited()


def test_allow_notification_uses_recipient_language(monkeypatch):
    from tgforward.handlers import admin
    from tgforward.ui import state

    async def run():
        st = state.set(1, "admin", "allow")
        client = NS(send_message=AsyncMock())
        message = NS(from_user=NS(id=1), reply=AsyncMock())
        monkeypatch.setattr(admin, "set_whitelisted", AsyncMock(return_value=True))
        monkeypatch.setattr(users, "get_user", AsyncMock(return_value={"ui_language": "en"}))
        try:
            with language_context("zh"):
                await admin._apply_user_change.__wrapped__(client, message, "9876")
                assert current_language() == "zh"
            assert HAN.search(message.reply.call_args.args[0])
            assert not HAN.search(client.send_message.call_args.args[1])
        finally:
            if state.get(1) is st:
                state.clear(1)

    asyncio.run(run())
