"""先校验配置，再在唯一事件循环内初始化数据库、客户端和处理器。"""

import asyncio
import logging
import os
import signal
import time
from contextlib import suppress
from importlib import import_module

from tgforward.config import validate_config

logger = logging.getLogger("tgforward.app")
HEARTBEAT_FILE = os.path.join(os.getcwd(), ".heartbeat")
READY_FILE = os.path.join(os.getcwd(), ".ready")
HEARTBEAT_INTERVAL = 30


def _check_config():
    problems = validate_config()
    for problem in problems:
        logger.error("配置缺失或非法：%s", problem)
    return not problems


def load_handlers():
    """只在已设置生产事件循环且配置已通过后调用。"""
    import_module("tgforward.handlers")


async def _heartbeat(watchdog):
    while True:
        watchdog.beat()
        try:
            with open(HEARTBEAT_FILE, "w") as file:
                file.write(str(time.time()))
        except OSError:
            logger.warning("心跳文件写入失败；内存 watchdog 仍在工作")
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def main() -> int:
    if not _check_config():
        return 1
    from tgforward.runtime.watchdog import Watchdog

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    cleanup = []
    heartbeat_task = None
    watchdog = Watchdog()
    try:
        for marker in (READY_FILE, HEARTBEAT_FILE):
            with suppress(OSError):
                os.remove(marker)
        watchdog.start()
        heartbeat_task = asyncio.create_task(_heartbeat(watchdog))
        storage = import_module("tgforward.storage.sqlite")
        cleanup.insert(0, storage.close)
        await storage.initialize()

        clients = import_module("tgforward.telegram.clients")
        cleanup.insert(0, clients.stop_all_clients)
        ui = import_module("tgforward.ui.interaction")
        cleanup.insert(0, ui.shutdown)
        dialogue = import_module("tgforward.ui.dialogue")
        cleanup.insert(0, dialogue.shutdown)
        tasks = import_module("tgforward.runtime.tasks")
        cleanup.insert(0, tasks.shutdown)
        cleanup.insert(1, import_module("tgforward.transfers.delivery").shutdown)
        load_handlers()
        cleanup.insert(0, import_module("tgforward.handlers.router").shutdown)
        await clients.start_main_clients()
        try:
            await import_module("tgforward.handlers.start").configure_menu()
        except Exception as exc:
            logger.warning("注册机器人命令失败 type=%s，可用 /set 重试", type(exc).__name__)
        with open(READY_FILE, "w") as file:
            file.write(str(os.getpid()))
        await stop_event.wait()
        return 0
    except Exception:
        logger.exception("启动或运行失败，开始回收资源")
        return 1
    finally:
        with suppress(OSError):
            os.remove(READY_FILE)
        watchdog.close()
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        # 某一步清理失败也必须继续关闭其他资源，尤其是 SQLite。
        for close in cleanup:
            try:
                await asyncio.wait_for(close(), timeout=45)
            except (Exception, asyncio.CancelledError) as exc:
                logger.error("资源关闭失败 %s type=%s", close.__name__, type(exc).__name__)
        for sig in installed:
            loop.remove_signal_handler(sig)
        with suppress(OSError):
            os.remove(HEARTBEAT_FILE)


def run() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # 配置通过后才导入处理器、打开状态库并创建 Telegram 客户端。
    if not _check_config():
        return 1
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Client 和 Dispatcher 的构造、注册及 worker 都绑定这一条循环。
        return loop.run_until_complete(main())
    except KeyboardInterrupt:
        return 0
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            with suppress(Exception, asyncio.CancelledError):
                loop.run_until_complete(
                    asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5)
                )
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        asyncio.set_event_loop(None)
