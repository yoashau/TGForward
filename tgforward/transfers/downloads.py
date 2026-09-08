"""按客户端串行下载；FloodWait 暂停该账号队列并重试同一文件。"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field

from pyrogram.errors import FloodWait

from tgforward.runtime import lifecycle
from tgforward.telegram.wait import acquire, heartbeat_sleep
from tgforward.transfers.results import SideEffectRole
from tgforward.ui.i18n import tr

logger = logging.getLogger(__name__)


@dataclass
class DownloadQueue:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    blocked_until: float = 0


def queue_for(client):
    queue = client.__dict__.get("_tf_download_queue")
    if queue is None:
        queue = client._tf_download_queue = DownloadQueue()
    return queue


async def _wait(queue, task, status):
    await heartbeat_sleep(
        queue.blocked_until - time.monotonic(),
        task,
        tr("等待 Telegram 下载限流解除"),
        status=status,
        notice=lambda remaining: tr(
            "downloads.flood_wait",
            math.ceil(remaining),
        ),
    )


async def download_media(client, message, *, task=None, status=None, **kwargs):
    queue = queue_for(client)
    async with acquire(queue.lock, task, tr("等待同账号下载队列")):
        while True:
            await _wait(queue, task, status)
            if task:
                task.check_cancel()
                task.touch(tr("下载媒体"))
            try:
                if task is not None:
                    lifecycle.authorize_side_effect(task, SideEffectRole.DOWNLOAD)
                return await client.download_media(message, **kwargs)
            except FloodWait as exc:
                seconds = max(0, exc.value) + 1
                queue.blocked_until = max(queue.blocked_until, time.monotonic() + seconds)
                logger.warning(
                    "下载限流 account=%s message=%s wait=%s; retry=same_media",
                    getattr(client, "name", "client"),
                    getattr(message, "id", None),
                    seconds,
                )
                # 不向外层传递为单条失败，也不推进到下一条；取消后仍保留账号冷却时间。
