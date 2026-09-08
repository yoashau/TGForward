"""只验证统一菜单的导航、表单输入、返回与过期结果。"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from tgforward.handlers import admin, auth, common, menu, relay, settings, start
from tgforward.ui import interaction as ui
from tgforward.ui import panel, state
from tgforward.ui.i18n import normalize_language


def msg(mid=1, text="", uid=1):
    m = NS(
        id=mid,
        from_user=NS(id=uid),
        chat=NS(id=uid),
        text=text,
        photo=None,
        command=text[1:].split() if text.startswith("/") else None,
        edit=AsyncMock(),
        delete=AsyncMock(),
    )
    m.reply = AsyncMock(
        return_value=NS(id=mid + 10, chat=m.chat, edit=AsyncMock(), delete=AsyncMock())
    )
    return m


def query(m, data):
    return NS(from_user=NS(id=1), message=m, data=data, answer=AsyncMock())


def buttons(m):
    return [
        b.callback_data
        for row in m.edit.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    state._states.clear()
    for mod in [menu, settings, common]:
        monkeypatch.setattr(mod, "is_whitelisted", AsyncMock(return_value=True))
    for mod in [settings, admin, start]:
        monkeypatch.setattr(mod, "get_user", AsyncMock(return_value={}))
    monkeypatch.setattr(settings, "set_field", AsyncMock(return_value=True))
    monkeypatch.setattr(settings, "toggle_auto_comments", AsyncMock(return_value=True))
    monkeypatch.setattr(settings, "reset_settings", AsyncMock(return_value=True))
    monkeypatch.setattr(settings, "remove_custom_thumb", lambda _: "removed")
    yield
    state._states.clear()


def test_single_command_and_usage_on_home():
    assert [c.command for c in start.BOT_COMMANDS] == ["start", "setting", "account"]
    text, markup = menu.page("home", 1)
    assert "链接" in text and "评论" in text
    assert "nav:extract" not in [b.callback_data for r in markup.inline_keyboard for b in r]


@pytest.mark.parametrize(
    "category,parent",
    [("home", "nav:home"), ("transfer", "set:page:home"), ("content", "set:page:home")],
)
def test_one_parent_per_settings_page(category, parent):
    kb = settings.settings_keyboard(category)
    back = [b.callback_data for r in kb.inline_keyboard for b in r if "返回" in b.text]
    assert back == [parent]
    assert kb.inline_keyboard[-1][0].callback_data == "nav:close"


def test_special_buttons_use_emoji_without_color_api():
    kb = menu.keyboard([[("重置", "set:reset")]], "account")
    assert kb.inline_keyboard[0][0].text.startswith("♻️")
    assert kb.inline_keyboard[-2][0].text == "↩️ 返回"
    assert not hasattr(panel, "apply_style")


@pytest.mark.parametrize(
    "action", ["settings", "do:me", "do:history", "account", "telegram", "helper"]
)
def test_all_navigation_edits_existing_message(action):
    m = msg()

    async def check():
        await menu.navigate(None, query(m, "nav:" + action))
        m.reply.assert_not_awaited()
        m.delete.assert_not_awaited()
        m.edit.assert_awaited()
        assert "nav:close" in buttons(m)
        assert len([b for b in buttons(m) if b == "nav:account" or b == "nav:home"]) == 1
        await ui.shutdown()

    asyncio.run(check())


@pytest.mark.parametrize(
    "action",
    ["page:transfer", "page:content", "mode", "mode:auto", "comments", "reset", "remthumb"],
)
def test_all_settings_actions_stay_in_menu(action):
    m = msg()

    async def check():
        await settings.settings_callback(None, query(m, "set:" + action))
        m.reply.assert_not_awaited()
        m.delete.assert_not_awaited()
        assert "nav:close" in buttons(m)
        await ui.shutdown()

    asyncio.run(check())


@pytest.mark.parametrize(
    "action,kind,input_text",
    [
        ("login", "login", "bad phone"),
        ("bindbot", "helper", "bad token"),
        ("allow", "admin", "not an id"),
        ("ban", "admin", "not an id"),
    ],
)
def test_dialogue_errors_edit_original_and_delete_input(action, kind, input_text):
    m = msg()
    input_msg = msg(2, input_text)
    handlers = {
        "login": auth.handle_login_steps,
        "helper": relay.bind_bot_input,
        "admin": admin.admin_user_input,
    }

    async def check():
        await menu.navigate(None, query(m, "nav:do:" + action))
        assert state.get(1).kind == kind
        before = m.edit.await_count
        await handlers[kind](None, input_msg)
        assert m.edit.await_count > before
        input_msg.delete.assert_awaited()
        input_msg.reply.assert_not_awaited()
        m.delete.assert_not_awaited()
        assert "nav:close" in buttons(m)
        await menu.navigate(None, query(m, "nav:account"))
        assert state.get(1) is None
        m.delete.assert_not_awaited()
        await ui.shutdown()

    asyncio.run(check())


def test_setting_success_keeps_back_and_close():
    m, value = msg(), msg(2, "my label")

    async def check():
        await settings.settings_callback(None, query(m, "set:rename"))
        await settings.handle_settings_input(None, value)
        assert state.get(1) is None
        value.delete.assert_awaited()
        value.reply.assert_not_awaited()
        assert buttons(m) == ["set:page:content", "nav:close"]
        assert "已设置" in m.edit.call_args.args[0]
        await ui.shutdown()

    asyncio.run(check())


def test_stale_reply_cannot_overwrite_new_page():
    m = msg()

    async def check():
        p = panel.acquire(1, m)
        view = ui.MessageView(m, ui.Messages())
        view._panel, view._revision = p, p.revision
        p.revision += 1
        await view.reply("stale result")
        m.edit.assert_not_awaited()
        await ui.shutdown()

    asyncio.run(check())


def test_close_removes_menu_and_state():
    m = msg()

    async def check():
        await menu.navigate(None, query(m, "nav:do:login"))
        await menu.navigate(None, query(m, "nav:close"))
        assert state.get(1) is None
        assert 1 not in panel._panels
        m.delete.assert_awaited_once()
        await ui.shutdown()

    asyncio.run(check())


def test_menu_pages_have_no_input_footer():
    async def check():
        message = msg()
        screen = panel.Panel(1, message)
        for name in ("home", "account", "telegram", "helper", "admin"):
            text, markup = menu.page(name, 1)
            await screen.render(message, text, markup)
            rendered = message.edit.call_args.args[0]
            assert "─" not in rendered and "\n\n\n" not in rendered
            assert rendered == text.strip()
            assert "填写时" not in rendered
        assert "？" not in menu.page("home", 1)[0]

    asyncio.run(check())


def test_explicit_navigation_none_and_preserve():
    async def check():
        message = msg()
        screen = panel.Panel(1, message)
        await screen.render(message, "processing", navigation="none")
        assert message.edit.call_args.kwargs["reply_markup"] is None
        markup = menu.page("account", 1)[1]
        await screen.render(message, "account", markup)
        await screen.render(message, "updated", navigation="preserve")
        assert message.edit.call_args.kwargs["reply_markup"] is markup

    asyncio.run(check())


@pytest.mark.parametrize(
    "code,expected",
    [(None, "zh"), ("zh-hans", "zh"), ("zh-hant", "zh"), ("en", "en"), ("de", "en")],
)
def test_home_language_uses_telegram_locale(code, expected):
    assert normalize_language(code) == expected


def test_english_home_preserves_navigation_and_admin_visibility(monkeypatch):
    monkeypatch.setattr(menu, "OWNER_ID", [1])
    for uid in (1, 2):
        text, markup = menu.page("home", uid, language="en")
        labels = {b.callback_data: b.text for row in markup.inline_keyboard for b in row}
        assert "Welcome to TGForward" in text
        assert "https://t.me/channel/100 10" in text
        assert labels["nav:settings"] == "⚙️ Extraction settings"
        assert labels["nav:close"] == "✖️ Close menu"
        assert labels["nav:language"] == "🌐 Language"
        assert ("nav:admin" in labels) == (uid == 1)


def test_home_language_switch_survives_submenu_navigation(monkeypatch):
    from tgforward.storage import users

    monkeypatch.setattr(users, "set_ui_language", AsyncMock(return_value=True))
    monkeypatch.setattr(start, "configure_user_commands", AsyncMock())
    m = msg()

    async def check():
        await menu.navigate(None, query(m, "nav:home:en"))
        assert "Welcome to TGForward" in m.edit.call_args.args[0]
        await menu.navigate(None, query(m, "nav:account"))
        await menu.navigate(None, query(m, "nav:home"))
        assert "Welcome to TGForward" in m.edit.call_args.args[0]
        await menu.navigate(None, query(m, "nav:home:zh"))
        assert "欢迎使用" in m.edit.call_args.args[0]
        assert "nav:language" in buttons(m)

    asyncio.run(check())


def test_english_home_still_requires_access(monkeypatch):
    monkeypatch.setattr(menu, "is_whitelisted", AsyncMock(return_value=False))
    m = msg()
    q = query(m, "nav:home:en")
    asyncio.run(menu.navigate(None, q))
    m.edit.assert_not_awaited()
    assert q.answer.call_args.kwargs["show_alert"] is True
