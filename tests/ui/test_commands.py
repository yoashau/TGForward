"""实际分发顺序：专用命令优先于通用私聊路由。"""

import os
import subprocess
import sys
from pathlib import Path


def test_command_dispatch():
    result = subprocess.run(
        [sys.executable, __file__, "--probe", str(Path(__file__).resolve().parents[2])],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _probe(root):
    # 子进程使用生产入口的循环和真实注册顺序，不连接 Telegram / 状态数据库。
    sys.path.insert(0, root)
    os.environ.update(
        API_ID="12345",
        API_HASH="test",
        BOT_TOKEN="123456:test",
        OWNER_ID="1",
        STRING="",
        MASTER_KEY="dispatch-test-independent-master-key-0123456789",
        SALT_KEY="dispatch-test-independent-salt",
    )
    import asyncio
    import logging
    from unittest.mock import AsyncMock, patch

    from pyrogram import raw, types
    from pyrogram.handlers import MessageHandler

    from tgforward import app as main

    logging.getLogger("pyrogram.dispatcher").setLevel(logging.WARNING)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def check():
        assert main._check_config()
        main.load_handlers()
        from tgforward.telegram.clients import bot

        dispatcher = bot.dispatcher
        assert bot.loop is dispatcher.loop is asyncio.get_running_loop()
        await asyncio.sleep(0)  # 等待真实装饰器提交的注册任务完成。
        selected = []
        replies = []

        def record(callback):
            async def wrapped(client, message):
                selected.append(callback.__name__)
                if callback.__name__ == "start_handler":
                    await callback(client, message)

            return wrapped

        for group in dispatcher.groups.values():
            for handler in group:
                if isinstance(handler, MessageHandler) and hasattr(handler, "original_callback"):
                    handler.original_callback = record(handler.original_callback)

        async def reply(message, text, **kwargs):
            replies.append(text)
            return types.Message(id=999, chat=message.chat, client=bot)

        async def edit(message, text, **kwargs):
            replies.append(text)
            return message

        bot.me = types.User(id=123456, is_bot=True, first_name="Test", username="test_bot")
        bot.is_connected = True
        bot.invoke = AsyncMock(side_effect=AssertionError("unexpected Telegram RPC"))
        await bot.storage.open()
        await bot.storage.user_id(123456)
        await bot.storage.is_bot(True)
        cases = [
            ("/start", "start_handler"),
            ("/start@test_bot", "start_handler"),
            ("/help", "help_handler"),
            ("/id", "id_handler"),
            ("/history", "history_handler"),
            ("/set", "set_commands"),
            ("/setting", "settings_command"),
            ("https://t.me/example/1", "smart_router"),
        ]
        failures = []
        try:
            with (
                patch.object(bot, "recover_gaps", new=AsyncMock()),
                patch(
                    "tgforward.handlers.start.ensure_whitelisted", new=AsyncMock(return_value=True)
                ),
                patch.object(types.Message, "reply", new=reply),
                patch.object(types.Message, "edit", new=edit),
                patch.object(types.Message, "delete", new=AsyncMock()),
                patch("tgforward.ui.panel.dismiss_keyboard", new=AsyncMock()),
            ):
                await dispatcher.start()
                assert all(t.get_loop() is loop for t in dispatcher.handler_worker_tasks)
                for index, (text, expected) in enumerate(cases, 1):
                    selected.clear()
                    update = raw.types.Updates(
                        updates=[
                            raw.types.UpdateNewMessage(
                                message=raw.types.Message(
                                    id=index,
                                    peer_id=raw.types.PeerUser(user_id=123456),
                                    from_id=raw.types.PeerUser(user_id=1),
                                    date=1,
                                    message=text,
                                    entities=[],
                                ),
                                pts=index,
                                pts_count=1,
                            )
                        ],
                        users=[
                            raw.types.User(
                                id=1,
                                first_name="Sender",
                                access_hash=1,
                                usernames=[],
                                restriction_reason=[],
                            ),
                            raw.types.User(
                                id=123456,
                                first_name="Test",
                                bot=True,
                                access_hash=2,
                                usernames=[],
                                restriction_reason=[],
                            ),
                        ],
                        chats=[],
                        date=1,
                        seq=index,
                    )
                    await bot.handle_updates(update)
                    await asyncio.wait_for(dispatcher.updates_queue.join(), timeout=2)
                    if selected != [expected]:
                        failures.append(f"{text}: {selected} != {[expected]}")
                if len(replies) != 2 or not all("欢迎使用" in text for text in replies):
                    failures.append(f"start replies: {len(replies)} != 2")
        finally:
            await dispatcher.stop()
            await bot.storage.close()
            await main.import_module("tgforward.storage.sqlite").close()
        print(
            f"dispatch={'FAIL' if failures else 'PASS'}; cases={len(cases)}; "
            f"start_replies={len(replies)}; same_loop=PASS; shutdown=PASS"
        )
        for failure in failures:
            print(failure)
        return int(bool(failures))

    try:
        return loop.run_until_complete(check())
    finally:
        loop.close()


if __name__ == "__main__":
    raise SystemExit(_probe(sys.argv[2]))
