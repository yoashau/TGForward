"""手动评论读取、传输、终态在同一条管理消息中可见。"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from tgforward.comments.actions import CommentButton
from tgforward.runtime.tasks import Task
from tgforward.transfers.progress import result_keyboard
from tgforward.ui.keyboards import CommentAction


def fixture():
    task = Task(42, "comments", 1)
    client = NS(edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
    message = NS(
        id=101,
        chat=NS(id=42),
        text="✅ 原帖已完成\n\n📦 当前链接：媒体 8 个，已发送 8 个",
        reply_markup=result_keyboard(task, "success", CommentAction("source", 7, 12)),
    )
    return client, message, task


def test_comment_edit_really_updates_management_text():
    client, message, _ = fixture()
    status = CommentButton(client, message)
    asyncio.run(status.edit("🔎 正在读取评论区……"))
    client.edit_message_text.assert_awaited_once()
    args = client.edit_message_text.call_args.args
    assert args[:2] == (42, 101)
    assert message.text in args[2] and "正在读取评论区" in args[2]


def test_comment_finish_displays_summary():
    client, message, _ = fixture()
    status = CommentButton(client, message)
    asyncio.run(status.finish("评论提取：成功 9 条，失败 3 条。\n最后错误：read failed", "partial"))
    client.edit_message_text.assert_awaited_once()
    call = client.edit_message_text.call_args
    assert "成功 9 条，失败 3 条" in call.args[2] and "read failed" in call.args[2]
    row = call.kwargs["reply_markup"].inline_keyboard[0]
    assert row[0].text == "✅ 提取消息成功"
    assert "部分未完成" in row[1].text


def test_transfer_progress_preserves_original_result_and_single_media_counter():
    from tgforward.transfers.progress import TaskStatus, make_progress

    client, message, task = fixture()
    task.media_scope = "本次评论"
    task.media_known.add((99, 1))
    task.media_downloads.add((99, 1))
    task.media_downloaded.add((99, 1))
    status = CommentButton(client, message, task=task)
    assert TaskStatus.wrap(status, task) is status

    async def check():
        await status.finish(outcome="running")
        for label in ["⬇️ 下载中", "⬆️ 上传中"]:
            progress = make_progress(client, 42, 101, task, label=label, status_message=status)
            await progress(50 * 1048576, 100 * 1048576)
            call = client.edit_message_text.call_args
            assert call.args[2].startswith(message.text)
            assert label in call.args[2] and "50.00 MB / 100.00 MB" in call.args[2]
            assert "速度" in call.args[2] and "剩余时间" in call.args[2]
            assert call.args[2].count("📦 本次评论") == 1
            assert "需下载 1 个，已下载 1 个" in call.args[2]
            row = call.kwargs["reply_markup"].inline_keyboard[0]
            assert [b.callback_data for b in row] == [
                "flow:result:42:success",
                f"flow:cancel:42:{task.token}",
            ]
            assert row[1].text == "⏹ 停止提取评论"
        await status.finish("评论提取：成功 1 条，失败 0 条。")
        count = client.edit_message_text.await_count
        await progress(100 * 1048576, 100 * 1048576)
        await status.edit("迟到的卡住提示")
        assert client.edit_message_text.await_count == count

    asyncio.run(check())


def test_retry_replaces_comment_section_and_preserves_original_entities():
    from pyrogram import enums
    from pyrogram.types import MessageEntity

    from tgforward.comments.actions import SECTION

    client, message, task = fixture()
    original = message.text
    message.entities = [MessageEntity(type=enums.MessageEntityType.BOLD, offset=0, length=5)]
    message.text += SECTION + "旧的失败摘要"
    status = CommentButton(client, message, task=task)
    asyncio.run(status.finish(outcome="running"))
    call = client.edit_message_text.call_args
    text = call.args[2]
    assert text.startswith(original) and "旧的失败摘要" not in text
    assert text.count(SECTION) == 1
    assert call.kwargs["entities"][0].offset == 0
    assert call.kwargs["entities"][0].length == 5
    assert call.kwargs["parse_mode"] == enums.ParseMode.DISABLED


def test_comment_message_stays_within_telegram_limit():
    from tgforward.utils.text import utf16_len

    client, message, task = fixture()
    message.text = "✅" * 4000
    status = CommentButton(client, message, task=task)
    asyncio.run(status.finish("最后错误：" + "😀" * 3000, "failed"))
    assert utf16_len(client.edit_message_text.call_args.args[2]) <= 4096
    assert "最后错误" in client.edit_message_text.call_args.args[2]


def test_read_and_final_counts_use_shared_status_for_manual_and_automatic(monkeypatch):
    from tgforward.comments import discussion
    from tgforward.storage.users import UserSettings
    from tgforward.transfers.progress import TaskStatus

    client, message, task = fixture()
    monkeypatch.setattr(discussion, "collect", AsyncMock(return_value=([], False)))

    async def check():
        for status in [
            CommentButton(client, message, task=task),
            TaskStatus(NS(edit=AsyncMock()), task),
        ]:
            summary = await discussion.extract(
                NS(), NS(), "source", 7, UserSettings(42), 42, task, status
            )
            assert summary == "该帖子暂时没有评论。"
            calls = (
                client.edit_message_text.call_args_list
                if isinstance(status, CommentButton)
                else status.message.edit.call_args_list
            )
            texts = [
                call.args[2] if isinstance(status, CommentButton) else call.args[0]
                for call in calls
            ]
            assert "正在读取评论区" in texts[0]
            assert "已读取 0 条评论" in texts[1]
            assert summary in texts[-1]

    asyncio.run(check())


def test_manual_handler_keeps_real_discussion_summary_and_text_counts(monkeypatch):
    from tests.transfers.test_routing import _text_message
    from tests.ui.test_comment_actions import comment_fixture
    from tgforward.comments import discussion
    from tgforward.runtime import tasks
    from tgforward.transfers import transfer

    handler, client, query = comment_fixture(monkeypatch)
    query.message.text = "✅ 原帖提取完成"
    replies = [_text_message("one"), _text_message("two")]
    for mid, reply in enumerate(replies, 1):
        reply.id = mid
    monkeypatch.setattr(discussion, "collect", AsyncMock(return_value=(replies, False)))
    # The real transfer entry point handles the text sends; only Telegram RPCs are mocked.
    from tgforward.telegram import clients

    sender = NS(send_message=AsyncMock(return_value=NS(id=900)))
    monkeypatch.setattr(clients, "get_upload_bot", AsyncMock(return_value=sender))
    monkeypatch.setattr(transfer, "_MIN_SEND_INTERVAL", 0)
    monkeypatch.setattr(transfer, "_last_send_at", 0)
    monkeypatch.setattr(transfer, "_send_lock", asyncio.Lock())

    async def check():
        await handler(client, query)
        await tasks.get(42).runner
        sender.send_message.assert_awaited()
        assert sender.send_message.await_count == 2
        texts = [call.args[2] for call in client.edit_message_text.call_args_list]
        assert all(text.startswith(query.message.text) for text in texts)
        assert any("正在读取评论区" in text for text in texts)
        assert any("已读取 2 条评论" in text for text in texts)
        assert "成功 2 条，失败 0 条。" in texts[-1]
        assert texts[-1].count("💬 评论提取") == 1
        query.message.reply.assert_not_awaited()

    asyncio.run(check())


def test_watchdog_warning_and_timeout_are_visible(monkeypatch):
    from tests.ui.test_comment_actions import comment_fixture
    from tgforward.comments import discussion
    from tgforward.runtime import tasks

    handler, client, query = comment_fixture(monkeypatch)
    clock = [0.0]

    def monotonic():
        clock[0] += 0.006
        return clock[0]

    monkeypatch.setattr(tasks, "time", NS(monotonic=monotonic))
    monkeypatch.setattr(tasks, "TASK_STALL_TIMEOUT", 0.01)

    async def stalled(*args):
        args[-2].touch("读取评论区")
        await asyncio.Event().wait()

    monkeypatch.setattr(discussion, "extract", stalled)

    async def check():
        await handler(client, query)
        task = tasks.get(42)
        await asyncio.wait_for(task.runner, 2)
        texts = [call.args[2] for call in client.edit_message_text.call_args_list]
        assert any("暂时没有新进度" in text for text in texts)
        assert "长时间没有进度" in texts[-1]
        assert task.timed_out and not tasks.is_active(42)
        row = client.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard[0]
        assert row[0].text == "✅ 提取消息成功" and "重新提取" in row[1].text

    asyncio.run(check())


def test_text_comment_progress_is_throttled_but_visible(monkeypatch):
    from tests.transfers.test_routing import _text_message
    from tgforward.comments import discussion
    from tgforward.storage.users import UserSettings

    client, message, task = fixture()
    status = CommentButton(client, message, task=task)
    replies = [_text_message(str(i)) for i in range(3)]
    for i, item in enumerate(replies, 1):
        item.id = i
    monkeypatch.setattr(discussion, "collect", AsyncMock(return_value=(replies, False)))
    now = [0]
    monkeypatch.setattr(discussion, "time", NS(monotonic=lambda: now[0]))

    async def send(*args, **kwargs):
        now[0] += 3
        from tests.transfers.test_routing import confirmed_message

        return await confirmed_message(*args, **kwargs)

    monkeypatch.setattr(discussion.transfer, "transfer_message", send)
    asyncio.run(discussion.extract(NS(), NS(), "source", 7, UserSettings(42), 42, task, status))
    texts = [call.args[2] for call in client.edit_message_text.call_args_list]
    assert any("已发送 1 条，失败 0 条，待发送 2 条" in text for text in texts)
    assert "成功 3 条，失败 0 条" in texts[-1]


def test_cancel_button_stops_same_task_without_reply_and_restores_retry(monkeypatch):
    from tests.ui.test_comment_actions import comment_fixture
    from tgforward.comments import discussion
    from tgforward.handlers import cancel, comments
    from tgforward.runtime import tasks
    from tgforward.ui import interaction as ui
    from tgforward.ui import state

    _, client, query = comment_fixture(monkeypatch)
    query.message.delete = AsyncMock()
    query.message.text = "✅ 原帖已完成"
    monkeypatch.setattr(state, "_states", {})

    async def check():
        entered = asyncio.Event()
        original = ui.Messages()
        original.add(query.message)

        async def extract(*args):
            entered.set()
            await args[-2].wait_or_cancel(3600)

        monkeypatch.setattr(discussion, "extract", extract)
        try:
            await comments.on_fetch_comments(client, query)
            task = tasks.get(42)
            await entered.wait()
            markup = client.edit_message_text.call_args.kwargs["reply_markup"]
            stop = markup.inline_keyboard[0][1]
            assert stop.text == "⏹ 停止提取评论"
            assert stop.callback_data == f"flow:cancel:42:{task.token}"
            owner = ui._owners[(42, 101)]
            query.message.reply_markup = markup
            query.data = stop.callback_data
            await cancel.cancel_callback(client, query)
            assert ui._owners[(42, 101)] is owner
            await original.delete()
            query.message.delete.assert_not_awaited()
            await task.runner
            query.message.reply.assert_not_awaited()
            final = client.edit_message_text.call_args
            assert "评论提取已停止" in final.args[2]
            row = final.kwargs["reply_markup"].inline_keyboard[0]
            assert row[0].callback_data == "flow:result:42:success"
            assert row[1].callback_data == "cmt:source:7:42"
            assert row[1].text == "⏹ 已停止 · 重新提取"
            assert not tasks.is_active(42)

            # A stale stop token must never cancel the next task.
            next_task = tasks.register(42, "comments", 1)
            await cancel.cancel_callback(client, query)
            assert not next_task.cancel_requested
            assert "过期" in query.answer.call_args.args[0]
            tasks.finish(42, next_task)
        finally:
            await ui.shutdown()

    asyncio.run(check())


def test_foreign_user_stop_button_does_not_cancel_task(monkeypatch):
    from tgforward.handlers import cancel
    from tgforward.runtime import tasks

    monkeypatch.setattr(tasks, "_tasks", {})
    monkeypatch.setattr(tasks, "_last_finished", {})
    task = tasks.register(42, "comments", 1)
    query = NS(
        from_user=NS(id=99),
        data=f"flow:cancel:42:{task.token}",
        answer=AsyncMock(),
        message=NS(reply=AsyncMock()),
    )
    asyncio.run(cancel.cancel_callback.__wrapped__(None, query))
    assert not task.cancel_requested
    query.message.reply.assert_not_awaited()
    assert "其他用户" in query.answer.call_args.args[0]


def test_handler_failure_details_are_visible_and_retry_callback_restored(monkeypatch):
    from tests.ui.test_comment_actions import comment_fixture
    from tgforward.comments import discussion
    from tgforward.runtime import tasks
    from tgforward.transfers.transfer import TransferError

    handler, client, query = comment_fixture(monkeypatch)
    monkeypatch.setattr(
        discussion, "extract", AsyncMock(side_effect=TransferError("discussion unavailable"))
    )

    async def check():
        await handler(client, query)
        await tasks.get(42).runner
        final = client.edit_message_text.call_args
        assert "discussion unavailable" in final.args[2]
        row = final.kwargs["reply_markup"].inline_keyboard[0]
        assert row[0].text == "✅ 提取消息成功"
        assert row[1].text == "⚠️ 提取失败 · 重新提取"
        assert row[1].callback_data == "cmt:source:7:42"
        query.message.reply.assert_not_awaited()

    asyncio.run(check())


def test_terminal_state_waits_for_inflight_progress_then_ignores_late_update():
    client, message, task = fixture()
    status = CommentButton(client, message, task=task)

    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        async def edit(*args, **kwargs):
            if "old progress" in args[2]:
                entered.set()
                await release.wait()

        client.edit_message_text.side_effect = edit
        progress = asyncio.create_task(status.edit("old progress"))
        await entered.wait()
        finish = asyncio.create_task(status.finish("评论提取：成功 1 条，失败 0 条。"))
        release.set()
        await asyncio.gather(progress, finish)
        await status.edit("late progress")
        assert client.edit_message_text.await_count == 2
        assert "成功 1 条" in client.edit_message_text.call_args.args[2]

    asyncio.run(check())
