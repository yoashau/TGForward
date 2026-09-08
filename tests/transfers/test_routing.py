"""transfer_message 路由决策测试：用假客户端验证克隆/降级/相册的分支逻辑。"""

import asyncio
import os
from types import SimpleNamespace

import pytest
from pyrogram.errors import ChatForwardsRestricted

from tgforward.transfers.transfer import TransferError, transfer_message


def _settings(**kw):
    from tgforward.storage.users import UserSettings

    return UserSettings(user_id=1, **kw)


class FakeUploader:
    """记录调用的假上传客户端；未定义的方法被调用即失败。"""

    def __init__(self, fail_copy_message=False, fail_copy_media_group=False):
        self.calls = []
        self.fail_copy_message = fail_copy_message
        self.fail_copy_media_group = fail_copy_media_group

    async def send_message(self, *a, **k):
        self.calls.append(("send_message", k))
        return SimpleNamespace(id=1, chat=SimpleNamespace(id=1))

    async def copy_message(self, *a, **k):
        if self.fail_copy_message:
            raise ChatForwardsRestricted()
        self.calls.append(("copy_message", k))
        return SimpleNamespace(id=1, chat=SimpleNamespace(id=1))

    async def copy_media_group(self, *a, **k):
        if self.fail_copy_media_group:
            raise ChatForwardsRestricted()
        self.calls.append(("copy_media_group", k))
        return [SimpleNamespace(id=1, chat=SimpleNamespace(id=1))]

    async def send_media_group(self, chat_id, media, **kwargs):
        assert 2 <= len(media) <= 10, "send_media_group requires 2–10 items"
        thread = kwargs.get("message_thread_id")
        reply = kwargs.get("reply_to_message_id")
        assert thread is None or isinstance(thread, int) and thread > 0
        assert reply is None or isinstance(reply, int) and reply > 0
        self.calls.append(("send_media_group", kwargs))
        return [
            SimpleNamespace(id=i, chat=SimpleNamespace(id=chat_id))
            for i in range(1, len(media) + 1)
        ]

    async def send_message_status(self, *a, **k):  # 物理搬运状态消息
        return SimpleNamespace(id=9, chat=SimpleNamespace(id=1))

    def _record(self, name):
        async def _call(*a, **k):
            self.calls.append((name, k))
            return SimpleNamespace(id=1, chat=SimpleNamespace(id=a[0]))

        return _call

    async def send_document(self, *a, **k):
        return await self._record("send_document")(*a, **k)

    async def send_video(self, *a, **k):
        return await self._record("send_video")(*a, **k)

    async def send_photo(self, *a, **k):
        return await self._record("send_photo")(*a, **k)

    async def send_audio(self, *a, **k):
        return await self._record("send_audio")(*a, **k)

    async def send_animation(self, *a, **k):
        return await self._record("send_animation")(*a, **k)

    async def send_sticker(self, *a, **k):
        return await self._record("send_sticker")(*a, **k)

    async def send_voice(self, *a, **k):
        return await self._record("send_voice")(*a, **k)

    async def send_video_note(self, *a, **k):
        return await self._record("send_video_note")(*a, **k)


class FakeDownloader:
    def __init__(self, group=None, download_result=None):
        self._group = group
        self._download_result = download_result

    async def get_messages(self, chat_id, ids):
        return self._group or []

    async def download_media(self, message, **k):
        return self._download_result


def _text_message(text="hello"):
    return SimpleNamespace(
        text=text,
        media=None,
        media_group_id=None,
        caption=None,
        chat=SimpleNamespace(id=-100777),
        id=55,
    )


def _media_message(**kw):
    base = dict(
        text=None,
        media=SimpleNamespace(),
        media_group_id=None,
        caption=None,
        chat=SimpleNamespace(id=-100777),
        id=66,
        video=None,
        photo=None,
        audio=None,
        document=None,
        animation=None,
        sticker=None,
        voice=None,
        video_note=None,
        web_page=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def run(coro):
    return asyncio.run(coro)


class TestTextRouting:
    def test_text_goes_to_send_message(self):
        up = FakeUploader()
        out = run(
            transfer_message(
                up,
                FakeDownloader(),
                _text_message(),
                _settings(),
                "42",
                source_private=False,
            )
        )
        kinds = [c[0] for c in up.calls]
        assert kinds == ["send_message"]
        assert out.summary == "完成。"

    def test_text_rules_applied(self):
        up = FakeUploader()
        s = _settings(replacements={"hello": "你好"})
        run(transfer_message(up, FakeDownloader(), _text_message(), s, "42", source_private=False))
        # send_message 的位置参数第 3 个是文本
        assert up.calls[0][0] == "send_message"

    def test_long_text_split_into_chunks(self):
        up = FakeUploader()
        msg = _text_message("字" * 9000)
        run(transfer_message(up, FakeDownloader(), msg, _settings(), "42", source_private=False))
        assert len(up.calls) >= 3  # 9000 字分成多段


class TestAlbumRouting:
    def _album(self):
        m = _media_message(media_group_id="g1", photo=SimpleNamespace(file_size=1024))
        return m

    def test_public_album_uses_native_copy(self):
        up = FakeUploader()
        group = [self._album()]
        dl = FakeDownloader(group=group)
        out = run(
            transfer_message(
                up,
                dl,
                group[0],
                _settings(),
                "42",
                source_private=False,
            )
        )
        kinds = [c[0] for c in up.calls]
        assert kinds == ["copy_media_group"]
        assert out.summary == "完成：相册（1 项）"
        assert out.last_message_id == 66

    def test_album_rules_passed_via_captions(self):
        up = FakeUploader()
        base = vars(self._album())
        base["caption"] = "SPAM 图"
        m = SimpleNamespace(**base)
        s = _settings(replacements={"SPAM": ""})
        run(transfer_message(up, FakeDownloader(group=[m]), m, s, "42", source_private=False))
        kwargs = up.calls[0][1]
        assert kwargs["captions"] == [" 图"]

    def test_inaccessible_private_album_download_failure(self):
        up = FakeUploader(fail_copy_media_group=True)
        m = self._album()
        dl = FakeDownloader(group=[m], download_result=None)
        with pytest.raises(TransferError):
            # 复制因访问限制失败后才下载，下载失败 → 报错
            run(transfer_message(up, dl, m, _settings(), "42", source_private=True))
        assert all(c[0] != "copy_media_group" for c in up.calls)


class TestSingleMediaRouting:
    def test_public_single_uses_copy(self):
        up = FakeUploader()
        m = _media_message(document=SimpleNamespace(file_name="a.zip", file_size=2048))
        out = run(
            transfer_message(
                up,
                FakeDownloader(),
                m,
                _settings(),
                "42",
                source_private=False,
            )
        )
        kinds = [c[0] for c in up.calls]
        assert kinds == ["copy_message"]
        assert "文件" in out.summary

    def test_public_copy_failure_does_not_download(self, tmp_path):
        from unittest.mock import AsyncMock

        up = FakeUploader(fail_copy_message=True)
        m = _media_message(document=SimpleNamespace(file_name="a.zip", file_size=2))
        dl = FakeDownloader()
        dl.download_media = AsyncMock()
        with pytest.raises(TransferError, match="公开来源不下载"):
            run(transfer_message(up, dl, m, _settings(), "42", source_private=False))
        dl.download_media.assert_not_awaited()

    def test_inaccessible_private_single_goes_physical(self, tmp_path):
        f = tmp_path / "b.bin"
        f.write_bytes(b"x")
        up = FakeUploader(fail_copy_message=True)
        m = _media_message(document=SimpleNamespace(file_name="b.bin", file_size=2))
        dl = FakeDownloader(download_result=str(f))
        run(
            transfer_message(
                up,
                dl,
                m,
                _settings(),
                "42",
                source_private=True,
            )
        )
        assert all(c[0] != "copy_message" for c in up.calls)
        assert "send_document" in [c[0] for c in up.calls]
        assert not os.path.exists(str(f))  # 临时文件已清理

    def test_download_failure_raises_transfer_error(self):
        up = FakeUploader(fail_copy_message=True)
        m = _media_message(document=SimpleNamespace(file_name="x", file_size=1))
        dl = FakeDownloader(download_result=None)
        with pytest.raises(TransferError):
            run(transfer_message(up, dl, m, _settings(), "42", source_private=True))

    def test_copy_keeps_original_buttons_without_comment_injection(self):
        up = FakeUploader()

        run(
            transfer_message(
                up,
                FakeDownloader(),
                _media_message(document=SimpleNamespace(file_size=1)),
                _settings(),
                "42",
                source_private=False,
            )
        )
        assert "reply_markup" not in up.calls[0][1]

    def test_no_markup_kwarg_when_none(self):
        up = FakeUploader()
        run(
            transfer_message(
                up,
                FakeDownloader(),
                _media_message(document=SimpleNamespace(file_size=1)),
                _settings(),
                "42",
                source_private=False,
            )
        )
        assert "reply_markup" not in up.calls[0][1]


async def confirmed_physical(*args, **kwargs):
    from unittest.mock import AsyncMock

    from tgforward.transfers import transfer

    message = args[2]
    active = transfer._current_result.get()
    await transfer.rpc.execute(
        AsyncMock(return_value=SimpleNamespace(id=message.id)),
        None,
        active.delivery,
        [f"media:{message.id}"],
    )
    return transfer.TransferDescription("ok")


async def confirmed_message(*args, **kwargs):
    from tgforward.transfers.results import DeliveryPart, MessageResult

    members = kwargs.get("media_group") or [args[2]]
    result = MessageResult(str((args[2].chat.id, args[2].id)), "test")
    result.resolve_source([m.id for m in members], [DeliveryPart(str(m.id), m.id) for m in members])
    for part in result.delivery.all_parts:
        result.delivery.begin_attempt(part)
        result.delivery.confirm_delivered(part)
    return result
