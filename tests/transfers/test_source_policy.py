"""固定来源策略、数量进度、停止按钮与 topics 相册兼容。"""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pyrogram import raw, types, utils
from pyrogram.errors import ChannelInvalid, ChannelPrivate

from tests.transfers.test_routing import FakeDownloader, FakeUploader, _media_message
from tgforward.comments import discussion
from tgforward.runtime.tasks import Task
from tgforward.storage.users import UserSettings
from tgforward.transfers import transfer
from tgforward.transfers.progress import TaskStatus
from tgforward.ui import panel


@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize("album", [True, False])
def test_source_decides_transfer_policy(monkeypatch, private, album):
    m = _media_message(photo=NS(file_size=1), media_group_id="g" if album else None)
    up = FakeUploader()
    from tests.transfers.test_routing import confirmed_physical

    physical = AsyncMock(side_effect=confirmed_physical)
    monkeypatch.setattr(transfer, "_transfer_physical", physical)
    monkeypatch.setattr(transfer, "_send_album_physical", physical)
    asyncio.run(
        transfer.transfer_message(
            up,
            FakeDownloader(group=[m]),
            m,
            UserSettings(1, rename_tag="tag"),
            "1",
            source_private=private,
        )
    )
    assert physical.await_count == int(private)
    assert len(up.calls) == int(not private)


def test_channel_invalid_on_private_source_is_not_reached(monkeypatch):
    up = NS(copy_message=AsyncMock(side_effect=ChannelInvalid()))
    from tests.transfers.test_routing import confirmed_physical

    physical = AsyncMock(side_effect=confirmed_physical)
    monkeypatch.setattr(transfer, "_transfer_physical", physical)
    asyncio.run(
        transfer.transfer_message(
            up,
            None,
            _media_message(photo=NS(file_size=1)),
            UserSettings(1),
            "1",
            source_private=True,
        )
    )
    up.copy_message.assert_not_awaited()
    physical.assert_awaited_once()


def test_public_copy_failure_never_downloads(monkeypatch):
    up = NS(copy_message=AsyncMock(side_effect=ChannelInvalid()))
    physical = AsyncMock()
    monkeypatch.setattr(transfer, "_transfer_physical", physical)
    with pytest.raises(transfer.TransferError, match="公开来源不下载"):
        asyncio.run(
            transfer.transfer_message(
                up,
                None,
                _media_message(photo=NS(file_size=1)),
                UserSettings(1),
                "1",
                source_private=False,
            )
        )
    physical.assert_not_awaited()


def test_status_keeps_counts_and_stop_button_after_each_edit():
    task = Task(1, "batch", 3)
    m = _media_message(photo=NS(file_size=1))
    task.discover(m, True)
    task.discover(m, True)
    other = _media_message(photo=NS(file_size=1), id=67)
    task.discover(other, True)
    task.media_downloaded.add(task.media_key(m))
    status = NS(edit=AsyncMock())

    async def check():
        for text in ["下载", "上传", "重命名"]:
            await TaskStatus(status, task).edit(text)
            call = status.edit.call_args
            assert "需下载 2 个，已下载 1 个，待下载 1 个" in call.args[0]
            button = call.kwargs["reply_markup"].inline_keyboard[0][0]
            assert button.text == "⏹ 停止提取消息"
            assert button.callback_data.endswith(task.token)

    asyncio.run(check())


@pytest.mark.parametrize("method", ["send", "copy"])
def test_real_pyrofork_album_result_accepts_missing_topics(monkeypatch, method):
    from pyrogram.methods.messages.copy_media_group import CopyMediaGroup
    from pyrogram.methods.messages.send_media_group import SendMediaGroup

    from tgforward.telegram.compat import install

    install()
    source = NS(photo=NS(file_id="cached"), caption=None, has_media_spoiler=False)
    c = NS(
        resolve_peer=AsyncMock(return_value=raw.types.InputPeerSelf()),
        rnd_id=lambda: 1,
        parser=NS(parse=AsyncMock(return_value={"message": "", "entities": []})),
        get_media_group=AsyncMock(return_value=[source, source]),
        invoke=AsyncMock(return_value=NS(updates=[], users=[], chats=[])),
    )
    monkeypatch.setattr(
        utils, "get_input_media_from_file_id", lambda *a, **kw: raw.types.InputMediaEmpty()
    )
    monkeypatch.setattr(utils, "get_reply_to", AsyncMock(return_value=None))
    monkeypatch.setattr(
        utils, "parse_text_entities", AsyncMock(return_value={"message": "", "entities": []})
    )

    async def check():
        if method == "copy":
            result = await CopyMediaGroup.copy_media_group(c, 1, 2, 3)
        else:
            result = await SendMediaGroup.send_media_group(
                c, 1, [types.InputMediaPhoto("cached"), types.InputMediaPhoto("cached")]
            )
        assert list(result) == []
        assert c.invoke.await_count == 1  # 不靠重新发送相册绕过异常

    asyncio.run(check())


def test_topics_explicit_list_preserved():
    value = [NS(id=1)]
    container = raw.types.messages.Messages(messages=[], chats=[], users=[], topics=value)
    assert container.topics is value


def test_permission_error_identifies_read_stage():
    c = NS(
        get_messages=AsyncMock(return_value=NS(id=1)),
        resolve_peer=AsyncMock(
            return_value=raw.types.InputPeerChannel(channel_id=1, access_hash=2)
        ),
        invoke=AsyncMock(side_effect=ChannelPrivate()),
    )
    with pytest.raises(transfer.TransferError, match="确认相册评论入口.*ChannelPrivate"):
        asyncio.run(discussion.collect(c, "example", 1))
    text = discussion.explain_error(TypeError("topics"), "发送评论附件")
    assert "未将它判定为权限问题" in text


def test_old_keyboard_removed_instead_of_reinstalled():
    m = NS(from_user=NS(id=1), reply=AsyncMock(return_value=NS(delete=AsyncMock())))
    asyncio.run(panel.dismiss_keyboard(m))
    assert isinstance(m.reply.call_args.kwargs["reply_markup"], types.ReplyKeyboardRemove)
    m.reply.return_value.delete.assert_awaited_once()
