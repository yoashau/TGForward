from types import SimpleNamespace

from tgforward.storage.users import UserSettings
from tgforward.transfers.transfer import _album_captions, _describe_media, _human_size


def _msg(**kwargs):
    return SimpleNamespace(**kwargs)


def _cap(markdown):
    return markdown


def _settings(**kwargs) -> UserSettings:
    return UserSettings(user_id=1, **kwargs)


class TestAlbumCaptions:
    def test_no_rules_keeps_original(self):
        group = [_msg(caption=_cap("第一张")), _msg(caption=_cap("第二张"))]
        finals, originals = _album_captions(group, _settings())
        assert finals == [None, None]  # None = 原样保留
        assert originals == ["第一张", "第二张"]

    def test_captions_without_caption_object(self):
        group = [_msg(), _msg(caption=None)]
        finals, originals = _album_captions(group, _settings())
        assert finals == [None, None]
        assert originals == ["", ""]

    def test_user_caption_on_last_only(self):
        group = [_msg(caption=_cap("一张")), _msg(caption=_cap("两张"))]
        finals, _ = _album_captions(group, _settings(caption="预设文案"))
        assert finals[0] is None
        assert finals[1] == "两张\n\n预设文案"

    def test_replacement_applied_per_item(self):
        group = [_msg(caption=_cap("SPAM one")), _msg(caption=_cap("SPAM two"))]
        finals, _ = _album_captions(group, _settings(replacements={"SPAM": ""}))
        assert finals == [" one", " two"]

    def test_delete_to_empty_string(self):
        group = [_msg(caption=_cap("广告")), _msg(caption=None)]
        finals, _ = _album_captions(group, _settings(delete_words=["广告"]))
        assert finals == ["", None]  # 清空为 "" 会显式移除该 caption


class TestDescribeMedia:
    def test_video_with_name_and_size(self):
        m = _msg(video=SimpleNamespace(file_name="clip.mkv", file_size=2 * 1024**3))
        assert _describe_media(m) == "视频 `clip.mkv`（2.00 GB）"

    def test_photo_without_name(self):
        m = _msg(photo=SimpleNamespace(file_size=1536 * 1024))
        assert _describe_media(m) == "图片（1.50 MB）"

    def test_bare_message(self):
        assert _describe_media(_msg()) == "媒体"


class TestHumanSize:
    def test_bytes(self):
        assert _human_size(0) == "0 B"
        assert _human_size(512) == "512 B"

    def test_units(self):
        assert _human_size(1024) == "1.00 KB"
        assert _human_size(1048576) == "1.00 MB"
        assert _human_size(1024**3) == "1.00 GB"
