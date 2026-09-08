"""一次输入多个链接共享同一 Task 时的计数验证。

全局 Task 的 current/success 必须单调累加；每个链接的批量进度与结果摘要
只能使用该链接的局部计数，避免“成功 4/2”与 /status 进度回退。
"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from tgforward.runtime import tasks
from tgforward.storage.users import UserSettings
from tgforward.telegram import clients
from tgforward.transfers import extractor
from tgforward.transfers.transfer import TransferDescription, TransferError
from tgforward.utils.links import MessageLink


def _make_status():
    edits = []

    async def edit(text, **kwargs):
        edits.append((text, kwargs.get("reply_markup")))

    return NS(id=1, chat=NS(id=1), edit=AsyncMock(side_effect=edit), delete=AsyncMock()), edits


def _media_msg(chat, mid):
    return NS(
        id=mid,
        chat=NS(id=chat),
        media_group_id=None,
        empty=False,
        photo=NS(file_size=1),
        video=None,
        audio=None,
        document=None,
        animation=None,
        voice=None,
        video_note=None,
        sticker=None,
        caption=None,
        text=None,
    )


@pytest.fixture(autouse=True)
def _patch_common(monkeypatch):
    messages = {
        ("chanA", 1): _media_msg("chanA", 1),
        ("chanA", 2): _media_msg("chanA", 2),
        ("chanB", 10): _media_msg("chanB", 10),
        ("chanB", 11): _media_msg("chanB", 11),
    }

    async def fetch(uploader, user_client, ref, message_id=None, user_id=None):
        return messages.get((ref.chat, message_id))

    monkeypatch.setattr(extractor, "fetch_message", fetch)
    monkeypatch.setattr(clients, "get_user_client", AsyncMock(return_value=None))
    monkeypatch.setattr(clients, "get_upload_bot", AsyncMock(return_value=NS()))
    monkeypatch.setattr(extractor, "load_user_settings", AsyncMock(return_value=UserSettings(1)))
    monkeypatch.setattr(extractor, "_comment_metadata", AsyncMock(return_value=None))
    monkeypatch.setattr(extractor, "record_extract_success", AsyncMock())
    monkeypatch.setattr(extractor, "BATCH_DELAY", 0)
    monkeypatch.setattr(tasks, "_tasks", {})
    monkeypatch.setattr(tasks, "_last_finished", {})
    return messages


def _request(statuses):
    return NS(
        from_user=NS(id=1),
        chat=NS(id=1),
        reply=AsyncMock(side_effect=statuses),
    )


async def _run_two_links(request, task):
    # 模拟 router._run_plan：同一全局 task 依次跑两个链接的批量提取。
    await extractor.extract_range(request, MessageLink("chanA", 1, False), 2, task=task)
    await extractor.extract_range(request, MessageLink("chanB", 10, False), 2, task=task)


def _final(edits):
    """TaskStatus.finish 最终也走 message.edit，最后一次编辑即结果摘要。"""
    return edits[-1]


def test_multilink_global_counters_and_local_summaries(monkeypatch):
    from tgforward.transfers import transfer as transfer_mod

    monkeypatch.setattr(
        transfer_mod, "transfer_message", AsyncMock(return_value=TransferDescription("完成：图片"))
    )
    s1, e1 = _make_status()
    s2, e2 = _make_status()
    request = _request([s1, s2])
    task = tasks.Task(1, "batch", 4)

    asyncio.run(_run_two_links(request, task))

    # 全局计数单调累加，不回退、不重置。
    assert (task.current, task.success) == (4, 4)
    # 每个链接的结果摘要使用自己的局部分母。
    assert "成功 2/2" in _final(e1)[0]
    assert "成功 2/2" in _final(e2)[0]
    # 任何状态文本都不得把全局累计值错配到局部分母。
    for edits in (e1, e2):
        for text, _ in edits:
            assert "4/2" not in text
            assert "已成功 4" not in text


def test_multilink_second_link_partial_failure_keeps_local_counts(monkeypatch):
    from tgforward.transfers import transfer as transfer_mod

    async def transfer_message(uploader, downloader, msg, *a, **kw):
        if (msg.chat.id, msg.id) == ("chanB", 10):
            raise TransferError("boom")
        return TransferDescription("完成：图片")

    monkeypatch.setattr(transfer_mod, "transfer_message", transfer_message)
    s1, e1 = _make_status()
    s2, e2 = _make_status()
    request = _request([s1, s2])
    task = tasks.Task(1, "batch", 4)

    asyncio.run(_run_two_links(request, task))

    # 全局：4 条全部处理过，3 条成功。
    assert (task.current, task.success) == (4, 3)
    final_text, final_markup = _final(e2)
    assert "成功 1/2" in final_text and "失败 1 条" in final_text
    # partial 结局按钮。
    assert "部分未完成" in final_markup.inline_keyboard[0][0].text
    # 第二批处理中的进度计数是局部的：失败先报 0 成功 1 失败，成功后报 1 成功。
    progress = [text for text, _ in e2[:-1]]
    assert any("已成功 0" in t and "失败 1" in t for t in progress)
    assert any("已成功 1" in t for t in progress)
    # 第一批不受影响，全部成功。
    assert "成功 2/2" in _final(e1)[0]
