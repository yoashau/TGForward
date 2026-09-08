"""交互、存储与任务编排的并发一致性。"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from tgforward.handlers import admin, auth, comments, relay, router, settings
from tgforward.runtime import lifecycle, tasks
from tgforward.storage import users
from tgforward.telegram import clients
from tgforward.ui import interaction as ui
from tgforward.ui import state
from tgforward.utils.links import MessageLink


def message(text="", uid=1):
    status = NS(id=90, chat=NS(id=uid), edit=AsyncMock(), delete=AsyncMock())
    return NS(
        id=1,
        chat=NS(id=uid),
        from_user=NS(id=uid),
        text=text,
        caption=None,
        command=text[1:].split() if text.startswith("/") else None,
        reply=AsyncMock(return_value=status),
        edit=AsyncMock(),
        delete=AsyncMock(),
        media_group_id=None,
        photo=None,
    )


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    for module, names in [
        (tasks, ("_tasks", "_last_finished")),
        (state, ("_states",)),
        (lifecycle, ("_locks", "_owners")),
        (clients, ("_user_clients", "_helper_bots", "_unclosed")),
    ]:
        for name in names:
            monkeypatch.setattr(module, name, {})
    monkeypatch.setattr(lifecycle, "revoked", set())
    monkeypatch.setattr(clients, "_closing", False)
    monkeypatch.setattr(clients, "_dialogs_cached", set())
    monkeypatch.setattr(router, "_recent_groups", {})
    for module in [auth, relay, admin]:
        monkeypatch.setattr(module, "ensure_whitelisted", AsyncMock(return_value=True))
    for module in [router, settings, comments]:
        monkeypatch.setattr(module, "is_whitelisted", AsyncMock(return_value=True))


@pytest.mark.parametrize("kind", ["user", "helper"])
def test_client_creation_is_single_flight_with_barrier(monkeypatch, kind):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        async def start(c):
            entered.set()
            await release.wait()
            return c

        made = []

        def factory(*a, **k):
            c = NS()
            made.append(c)
            return c

        monkeypatch.setattr(clients, "_client", factory)
        monkeypatch.setattr(clients, "safe_start_client", start)
        monkeypatch.setattr(
            users,
            "get_session" if kind == "user" else "get_helper_token",
            AsyncMock(return_value="credential"),
        )
        get = clients.get_user_client if kind == "user" else clients.get_helper_bot
        jobs = [asyncio.create_task(get(1)) for _ in range(12)]
        await entered.wait()
        await asyncio.sleep(0)
        assert len(made) == 1
        release.set()
        results = await asyncio.gather(*jobs)
        assert all(c is made[0] for c in results)

    asyncio.run(check())


def test_successful_login_invalidates_cached_session(monkeypatch):
    old = NS(is_initialized=True, is_connected=True, stop=AsyncMock())
    clients._user_clients[1] = old
    clients._dialogs_cached.add(1)
    monkeypatch.setattr(users, "set_field", AsyncMock(return_value=True))
    temp = NS(export_session_string=AsyncMock(return_value="new-session"))
    monkeypatch.setattr(auth, "_stop_quietly", AsyncMock())
    st = state.set(1, "login", "code")
    assert asyncio.run(auth._save_login(1, st, temp))
    assert 1 not in clients._user_clients and 1 not in clients._dialogs_cached
    old.stop.assert_awaited_once()
    assert users.set_field.call_args.args[1] == "session_string"


@pytest.mark.parametrize(
    "module,name",
    [(auth, "login_command"), (auth, "logout_command"), (relay, "bind_bot"), (relay, "unbind_bot")],
)
def test_active_task_blocks_lifecycle_commands(monkeypatch, module, name):
    calls = {}
    for key in [
        "get_session",
        "remove_session",
        "save_session",
        "remove_user_client",
        "get_helper_token",
        "remove_helper_token",
        "save_helper_token",
        "remove_helper_bot",
    ]:
        if hasattr(module, key):
            calls[key] = AsyncMock()
            monkeypatch.setattr(module, key, calls[key])

    async def check():
        task = tasks.register(1, "single", 1)
        m = message(
            "/"
            + {
                "login_command": "login",
                "logout_command": "logout",
                "bind_bot": "bindbot",
                "unbind_bot": "unbindbot",
            }[name]
        )
        await getattr(module, name)(None, m)
        assert tasks.get(1) is task and not task.cancelled
        assert state.get(1) is None
        assert "任务" in m.reply.call_args.args[0]
        for call in calls.values():
            call.assert_not_awaited()
        await ui.shutdown()

    asyncio.run(check())


@pytest.mark.parametrize(
    "name,args",
    [
        ("save_session", ("new",)),
        ("remove_session", ()),
        ("save_helper_token", ("new",)),
        ("remove_helper_token", ()),
    ],
)
def test_credential_storage_defends_active_task(monkeypatch, name, args):
    write, remove = AsyncMock(), AsyncMock()
    monkeypatch.setattr(users, "set_field", write)
    monkeypatch.setattr(users, "unset_field", remove)
    tasks.register(1, "single", 1)
    assert not asyncio.run(getattr(users, name)(1, *args))
    write.assert_not_awaited()
    remove.assert_not_awaited()


@pytest.mark.parametrize("kind", ["settings", "admin"])
def test_double_dialogue_input_consumed_once(monkeypatch, kind):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        writes = []

        async def write(*args):
            writes.append(args)
            entered.set()
            await release.wait()
            return True

        state.set(1, kind, "caption" if kind == "settings" else "allow")
        if kind == "settings":
            monkeypatch.setattr(settings, "set_field", write)
            handler = settings.handle_settings_input
            text = "caption"
        else:
            monkeypatch.setattr(admin, "set_whitelisted", write)
            handler = admin.admin_user_input
            text = "42"
        client = NS(send_message=AsyncMock())
        first = asyncio.create_task(handler(client, message(text)))
        await entered.wait()
        await asyncio.wait_for(handler(client, message(text)), timeout=0.3)
        assert len(writes) == 1
        release.set()
        await first
        assert state.get(1) is None
        await ui.shutdown()

    asyncio.run(check())


def test_cancel_before_launch_does_not_create_runner():
    async def check():
        task = tasks.register(1, "comments", 1)
        tasks.request_cancel(1)
        work = AsyncMock()
        tasks.launch(task, work, AsyncMock())
        await asyncio.sleep(0)
        work.assert_not_awaited()
        assert task.runner is None and not tasks.is_active(1)

    asyncio.run(check())


def test_domain_cancellation_is_not_failure(caplog):
    async def check():
        task = tasks.register(1, "comments", 1)
        notify = AsyncMock()
        tasks.launch(task, AsyncMock(side_effect=tasks.TaskCancelled()), notify)
        await task.runner
        assert "已停止" in notify.call_args.args[0]
        assert task.cancelled and not tasks.is_active(1)
        assert not [r for r in caplog.records if r.levelname == "ERROR"]

    asyncio.run(check())


def test_comment_cancel_in_register_launch_gap(monkeypatch):
    monkeypatch.setattr(comments, "_pick_session", AsyncMock(return_value=NS()))

    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        async def answer(*args, **kwargs):
            assert tasks.is_active(1)
            entered.set()
            await release.wait()

        from tgforward.transfers.progress import result_keyboard

        management = message()
        management.reply_markup = result_keyboard(tasks.Task(1, "single", 1), "success")
        q = NS(from_user=NS(id=1), message=management, data="cmt:channel:1", answer=answer)
        job = asyncio.create_task(comments.on_fetch_comments(None, q))
        await entered.wait()
        task = tasks.get(1)
        tasks.request_cancel(1)
        release.set()
        await job
        assert task.runner is None and not tasks.is_active(1)
        q.message.reply.assert_not_awaited()
        await ui.shutdown()

    asyncio.run(check())


def test_comment_answer_failure_releases_registration(monkeypatch):
    monkeypatch.setattr(comments, "_pick_session", AsyncMock(return_value=NS()))

    async def check():
        q = NS(
            from_user=NS(id=1),
            message=message(),
            data="cmt:channel:1",
            answer=AsyncMock(side_effect=RuntimeError("answer failed")),
        )
        from tgforward.transfers.progress import result_keyboard

        q.message.reply_markup = result_keyboard(tasks.Task(1, "single", 1), "success")
        with pytest.raises(RuntimeError):
            await comments.on_fetch_comments(None, q)
        assert not tasks.is_active(1)
        await ui.shutdown()

    asyncio.run(check())


@pytest.mark.parametrize(
    "raw,valid",
    [
        ("-100123", True),
        ("-100123/12", True),
        ("123", False),
        ("-123", False),
        ("-100", False),
        ("-1000", False),
        ("-100123/0", False),
        ("-100123/-1", False),
        ("-100123/2147483648", False),
        ("-100123/01", False),
        ("-100１２３", False),
        ("-100" + "9" * 50, False),
    ],
)
def test_target_id_matches_ui(monkeypatch, raw, valid):
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(settings, "set_field", save)
    assert asyncio.run(settings._handle_setchat(message(raw), 1)) is valid
    assert save.await_count == int(valid)


@pytest.mark.parametrize("kind", ["replacement", "delete", "toggle"])
def test_database_transactions_preserve_concurrent_updates(sqlite_store, kind):
    async def check():
        if kind == "replacement":
            result = await asyncio.gather(
                *(users.update_word_rules(1, replacement=(str(i), "x")) for i in range(8))
            )
            assert (await users.get_user(1))["replacement_words"] == dict.fromkeys(
                map(str, range(8)), "x"
            )
        elif kind == "delete":
            result = await asyncio.gather(
                *(users.update_word_rules(1, delete_words=[str(i)]) for i in range(8))
            )
            assert set((await users.get_user(1))["delete_words"]) == set(map(str, range(8)))
        else:
            result = await asyncio.gather(*(users.toggle_auto_comments(1) for _ in range(8)))
            assert (await users.get_user(1))["auto_comments"] is False
        assert all(x is not None for x in result)

    asyncio.run(check())


@pytest.mark.parametrize("direction", ["delete", "replacement", "existing"])
def test_rule_conflicts_checked_both_directions(sqlite_store, direction):
    doc = (
        {"replacement_words": {"spam": "x"}}
        if direction == "delete"
        else {"delete_words": ["spam"]}
    )
    if direction == "existing":
        doc["replacement_words"] = {"spam": "x"}

    async def check():
        await sqlite_store.mutate_user(1, lambda target: target.update(doc))
        before = await users.get_user(1)
        with pytest.raises(users.RuleConflict):
            if direction == "delete":
                await users.update_word_rules(1, delete_words=["spam"])
            else:
                await users.update_word_rules(
                    1, replacement=("spam" if direction == "replacement" else "other", "x")
                )
        assert await users.get_user(1) == before

    asyncio.run(check())


def test_opposing_rule_writes_cannot_both_succeed(sqlite_store):
    async def check():
        results = await asyncio.gather(
            users.update_word_rules(1, replacement=("spam", "x")),
            users.update_word_rules(1, delete_words=["spam"]),
            return_exceptions=True,
        )
        assert sum(r is True for r in results) == 1
        assert sum(isinstance(r, users.RuleConflict) for r in results) == 1

    asyncio.run(check())


def test_delete_words_preserve_order_and_stable_dedup(sqlite_store):
    asyncio.run(users.set_field(1, "delete_words", ["ab", "abc", "ab"]))
    assert asyncio.run(users.update_word_rules(1, delete_words=["bc", "abc"]))
    assert asyncio.run(users.get_field(1, "delete_words")) == ["ab", "abc", "bc"]


def test_reset_is_one_atomic_update(sqlite_store):
    async def check():
        await sqlite_store.mutate_user(
            1,
            lambda doc: doc.update(
                {"caption": "x", "delete_words": ["x"], "session_string": "keep"}
            ),
        )
        assert await users.reset_settings(1)
        assert await users.get_user(1) == {"user_id": 1, "session_string": "keep"}

    asyncio.run(check())


@pytest.mark.parametrize("link", ["https://t.me/MyChannel/42?single", "https://t.me/c/123/42"])
def test_forward_and_body_source_deduplicated(monkeypatch, link):
    plans = []

    def launch(task, work, notify):
        plans.append((task, work))

    monkeypatch.setattr(tasks, "launch", launch)
    captured = AsyncMock()
    monkeypatch.setattr(router, "_run_plan", captured)

    async def check():
        m = message(link + " 5")
        m.forward_from_chat = NS(id=-100123, username="mychannel")
        m.forward_from_message_id = 42
        await router.smart_router(None, m)
        task, work = plans[0]
        await work()
        assert task.total == 5
        assert len(captured.call_args.args[1]) == 1
        await ui.shutdown()

    asyncio.run(check())


def test_comments_are_not_deduplicated_with_post():
    refs = [MessageLink("Channel", 42, False), MessageLink("channel", 42, False, 7)]
    plan = router._deduplicate_plan([(r, 1, "url") for r in refs], message())
    assert len(plan) == 2


@pytest.mark.parametrize("failed", [False, True])
def test_logout_reports_remote_status_and_retry(monkeypatch, failed):
    candidate = NS(log_out=AsyncMock(side_effect=RuntimeError("network") if failed else None))
    monkeypatch.setattr(auth, "Client", lambda *a, **k: candidate)
    monkeypatch.setattr(auth, "get_session", AsyncMock(return_value="session"))
    monkeypatch.setattr(auth, "get_pending_logouts", AsyncMock(return_value=[]))
    monkeypatch.setattr(auth, "safe_connect_client", AsyncMock())
    monkeypatch.setattr(auth, "_stop_quietly", AsyncMock())
    monkeypatch.setattr(auth, "remove_session", AsyncMock(return_value=True))
    monkeypatch.setattr(auth, "remove_user_client", AsyncMock())
    record = AsyncMock(return_value=True)
    monkeypatch.setattr(auth, "record_logout_pending", record)
    monkeypatch.setattr(auth, "clear_logout_pending", AsyncMock(return_value=True))

    async def check():
        status = NS(edit=AsyncMock())
        await auth._logout_locked(1, status)
        text = status.edit.call_args.args[0]
        assert ("撤销失败" in text) is failed
        assert record.await_count == int(failed)
        auth.remove_session.assert_awaited_once()
        if failed:
            assert "本地已退出" in text and "/logout" in text
            monkeypatch.setattr(auth, "get_session", AsyncMock(return_value=None))
            monkeypatch.setattr(auth, "get_pending_logouts", AsyncMock(return_value=["session"]))
            candidate.log_out.side_effect = None
            await auth._logout_locked(1, status)
            assert "远端授权已撤销" in status.edit.call_args.args[0]
            auth.clear_logout_pending.assert_awaited_once()

    asyncio.run(check())


def test_pending_logout_credentials_are_encrypted(monkeypatch):
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(users, "set_field", save)
    assert asyncio.run(users.record_logout_pending(1, "secret-session", "TimeoutError"))
    _, key, value = save.call_args.args
    assert key.startswith("pending_logouts.") and "secret-session" not in key
    assert value["session_string"] != "secret-session"
    assert users.decrypt(value["session_string"]) == "secret-session"


def test_group_cache_has_global_capacity(monkeypatch):
    monkeypatch.setattr(router, "_GROUP_CAPACITY", 8)
    for uid in range(40):
        router._mark_group_handled(uid, "group")
    assert sum(map(len, router._recent_groups.values())) == 8
    assert not router._group_recently_handled(1, "group")
    assert router._group_recently_handled(39, "group")


def test_group_cache_expires_without_new_requests(monkeypatch):
    monkeypatch.setattr(router, "_GROUP_TTL", 0.01)

    async def check():
        router._mark_group_handled(1, "group")
        await asyncio.sleep(0.05)
        assert not router._recent_groups
        await router.shutdown()

    asyncio.run(check())


def test_router_registration_and_login_are_mutually_exclusive(monkeypatch):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        async def whitelist(_):
            entered.set()
            await release.wait()
            return True

        monkeypatch.setattr(router, "is_whitelisted", whitelist)
        monkeypatch.setattr(tasks, "launch", lambda *args: None)
        route = asyncio.create_task(router.smart_router(None, message("https://t.me/example/1")))
        await entered.wait()
        login_message = message("/login")
        login = asyncio.create_task(auth.login_command(None, login_message))
        await asyncio.sleep(0)
        assert not login.done()
        release.set()
        await asyncio.gather(route, login)
        assert tasks.is_active(1) and state.get(1) is None
        assert "任务" in login_message.reply.call_args.args[0]
        await ui.shutdown()

    asyncio.run(check())


def test_delayed_router_rechecks_dialogue_after_whitelist(monkeypatch):
    # 过滤器通过后，另一条命令仍可能先开始登录；函数体必须再次检查状态。
    state.set(1, "login", "phone")
    register = Mock(side_effect=AssertionError("router consumed dialogue input"))
    monkeypatch.setattr(tasks, "register", register)

    async def check():
        await router.smart_router(None, message("https://t.me/example/1"))
        register.assert_not_called()
        await ui.shutdown()

    asyncio.run(check())


def test_login_commit_checks_active_task(monkeypatch):
    tasks.register(1, "single", 1)
    st = state.set(1, "login", "code")
    temp = NS(export_session_string=AsyncMock(return_value="new"))
    save = AsyncMock()
    monkeypatch.setattr(auth, "save_session", save)
    monkeypatch.setattr(auth, "_stop_quietly", AsyncMock())
    assert not asyncio.run(auth._save_login(1, st, temp))
    temp.export_session_string.assert_not_awaited()
    save.assert_not_awaited()


def test_launch_duplicate_and_stale_task_are_ignored():
    async def check():
        task = tasks.register(1, "comments", 1)
        work = AsyncMock()
        tasks.launch(task, work, AsyncMock())
        runner = task.runner
        tasks.launch(task, work, AsyncMock())
        assert task.runner is runner
        await runner
        assert work.await_count == 1
        tasks._last_finished.clear()
        new = tasks.register(1, "comments", 1)
        tasks.launch(task, work, AsyncMock())
        assert tasks.get(1) is new and work.await_count == 1

    asyncio.run(check())


def test_failed_database_read_never_overwrites_rules(monkeypatch):
    mutate = AsyncMock(side_effect=RuntimeError("offline"))
    monkeypatch.setattr(users.storage, "mutate_user", mutate)
    assert not asyncio.run(users.update_word_rules(1, delete_words=["word"]))
    mutate.assert_awaited_once()


def test_logout_failed_retry_persistence_preserves_active_credential(monkeypatch):
    candidate = NS(log_out=AsyncMock(side_effect=RuntimeError("network")))
    monkeypatch.setattr(auth, "Client", lambda *a, **k: candidate)
    for name, value in [
        ("get_session", "session"),
        ("get_pending_logouts", []),
        ("record_logout_pending", False),
        ("remove_session", True),
    ]:
        monkeypatch.setattr(auth, name, AsyncMock(return_value=value))
    monkeypatch.setattr(auth, "safe_connect_client", AsyncMock())
    monkeypatch.setattr(auth, "_stop_quietly", AsyncMock())
    status = NS(edit=AsyncMock())
    asyncio.run(auth._logout_locked(1, status))
    auth.remove_session.assert_not_awaited()
    assert "原凭据保留" in status.edit.call_args.args[0]


def test_logout_already_revoked_is_success(monkeypatch):
    candidate = NS(log_out=AsyncMock(side_effect=auth.SessionRevoked()))
    monkeypatch.setattr(auth, "Client", lambda *a, **k: candidate)
    for name, value in [
        ("get_session", None),
        ("get_pending_logouts", ["old"]),
        ("clear_logout_pending", True),
        ("remove_user_client", None),
    ]:
        monkeypatch.setattr(auth, name, AsyncMock(return_value=value))
    monkeypatch.setattr(auth, "safe_connect_client", AsyncMock())
    monkeypatch.setattr(auth, "_stop_quietly", AsyncMock())
    status = NS(edit=AsyncMock())
    asyncio.run(auth._logout_locked(1, status))
    auth.clear_logout_pending.assert_awaited_once()
    assert "远端授权已撤销" in status.edit.call_args.args[0]


@pytest.mark.parametrize(
    "handler", [auth.login_command, auth.logout_command, relay.bind_bot, relay.unbind_bot]
)
def test_cancelled_runner_must_finish_cleanup_before_lifecycle_change(handler):
    async def check():
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def work():
            entered.set()
            try:
                await task.wait_or_cancel(3600)
            finally:
                cleaning.set()
                await release.wait()

        task = tasks.register(1, "single", 1)
        tasks.launch(task, work, AsyncMock())
        await entered.wait()
        tasks.request_cancel(1)
        await cleaning.wait()
        m = message("/lifecycle")
        await handler(None, m)
        assert tasks.is_active(1) and not task.runner.done()
        assert "任务" in m.reply.call_args.args[0]
        release.set()
        await task.runner
        assert not tasks.is_active(1)
        await ui.shutdown()

    asyncio.run(check())
