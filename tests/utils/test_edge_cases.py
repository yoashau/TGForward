"""文件冲突、资源回收与文本边界。"""

import asyncio
import os
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from tgforward.storage import crypto
from tgforward.transfers import transfer
from tgforward.utils import files, links, media, text


@pytest.mark.parametrize(
    "name",
    ["中文" * 130 + ".mkv", "😀" * 260 + ".zip", "x" * 400 + ".mp4"],
    ids=["chinese", "emoji", "ascii"],
)
def test_long_filename_byte_budget_and_extension(name):
    result = files.sanitize_filename(name)
    assert len(result.encode()) <= 250
    assert result.endswith(Path(name).suffix)


def test_name_collision_chooses_unique_path(tmp_path):
    source, occupied = tmp_path / "a.txt", tmp_path / "b.txt"
    source.write_bytes(b"new")
    occupied.write_bytes(b"old")
    result = files.apply_name_rules(str(source), [], {"a": "b"})
    assert result != str(source)
    assert occupied.read_bytes() == b"old"
    assert Path(result).read_bytes() == b"new"


def test_animation_participates_in_name_rules():
    assert transfer._has_original_name(NS(animation=NS(file_name="original.gif")))


def test_no_unrelated_whitespace_changes():
    original = "    code  =  1  \n\tline\n"
    assert text.apply_text_rules(original, {"never": "X"}, []) == original


def test_splitting_keeps_every_newline():
    original = "abc\n\n" * 5
    assert "".join(text.split_text(original, 10)) == original


@pytest.mark.parametrize(
    "url",
    [
        "https://t.me/foo/12evil",
        "https://t.me/foo/12/extra",
        "https://t.me/foo/12#fragment",
    ],
)
def test_illegal_url_tail_is_rejected(url):
    assert links.parse_link(url) is None


def test_finder_only_returns_parseable_urls():
    assert links.find_links("https://t.me/foo-bar/1") == []


def test_new_ciphertext_is_versioned():
    assert crypto.encrypt("test-session").startswith("v1:")


def test_unknown_video_metadata_not_fabricated(monkeypatch):
    monkeypatch.setattr(
        media.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError())
    )
    assert asyncio.run(media.get_video_metadata("missing")) is None


def test_missing_ffmpeg_degrades_and_cleans_file(monkeypatch, tmp_path):
    descriptors = []
    original = media.tempfile.mkstemp

    def mkstemp(*args, **kwargs):
        fd, name = original(*args, **kwargs)
        descriptors.append(fd)
        return fd, name

    monkeypatch.setattr(media.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(
        media.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError())
    )
    try:
        assert asyncio.run(media.screenshot("video", 1, str(tmp_path))) is None
        assert not list(tmp_path.glob("*.jpg"))
    finally:
        # 断言失败时也回收测试创建的文件描述符。
        for fd in descriptors:
            with suppress(OSError):
                os.close(fd)


def test_screenshot_mkstemp_fd_closed_before_spawn(monkeypatch, tmp_path):
    descriptors = []
    original = media.tempfile.mkstemp

    def mkstemp(*args, **kwargs):
        fd, name = original(*args, **kwargs)
        descriptors.append(fd)
        return fd, name

    async def spawn(*args, **kwargs):
        with pytest.raises(OSError):
            os.fstat(descriptors[0])
        raise FileNotFoundError()

    monkeypatch.setattr(media.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", spawn)
    try:
        assert asyncio.run(media.screenshot("video", 1, str(tmp_path))) is None
    finally:
        for fd in descriptors:
            with suppress(OSError):
                os.close(fd)
