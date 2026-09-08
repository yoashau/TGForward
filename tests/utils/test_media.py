"""媒体元数据、JPEG 规格与子进程资源管理。"""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from tgforward.utils import media


@pytest.fixture
def thumb_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "DATA_DIR", str(tmp_path / "persistent"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize("error", [FileNotFoundError(), PermissionError(), OSError("spawn")])
def test_missing_tools_degrade_and_remove_temp(monkeypatch, thumb_dir, error):
    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", AsyncMock(side_effect=error))
    assert asyncio.run(media.get_video_metadata("missing.mp4")) is None
    assert asyncio.run(media.screenshot("missing.mp4", None, str(thumb_dir))) is None
    assert not list(thumb_dir.glob("*.jpg"))


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
def test_subprocess_killed_and_reaped(monkeypatch, mode):
    async def check():
        entered, killed, reaped = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def communicate():
            entered.set()
            await killed.wait()
            reaped.set()
            return b"", b""

        process = NS(
            communicate=AsyncMock(side_effect=communicate),
            kill=Mock(side_effect=killed.set),
            returncode=-9,
        )
        monkeypatch.setattr(
            media.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
        )
        monkeypatch.setattr(media, "PROCESS_TIMEOUT", 0.01)
        task = asyncio.create_task(media._run_process("fixture"))
        await entered.wait()
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is None
        assert reaped.is_set()
        process.kill.assert_called_once()
        process.communicate.assert_awaited_once()

    asyncio.run(check())


@pytest.mark.parametrize(
    "payload,expected",
    [
        (b"not json", None),
        (b"null", None),
        (b'{"streams":[]}', None),
        (b'{"streams":[{"codec_type":"video","width":0}]}', None),
        (
            json.dumps(
                {
                    "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
                    "format": {"duration": "12.4"},
                }
            ).encode(),
            {"width": 1920, "height": 1080, "duration": 12},
        ),
        (
            b'{"streams":[{"codec_type":"video","width":640,"height":480}]}',
            {"width": 640, "height": 480},
        ),
        (b'{"streams":[{"codec_type":"video","duration":"NaN"}]}', None),
    ],
)
def test_metadata_unknown_and_valid(monkeypatch, payload, expected):
    monkeypatch.setattr(media, "_run_process", AsyncMock(return_value=payload))
    assert asyncio.run(media.get_video_metadata("file")) == expected


def test_nonzero_process_exit_is_failure(monkeypatch):
    process = NS(returncode=1, communicate=AsyncMock(return_value=(b"valid", b"failed")))
    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    assert asyncio.run(media._run_process("fixture")) is None


@pytest.mark.parametrize(
    "size,mode,fmt",
    [
        ((1920, 1080), "RGB", "JPEG"),
        ((700, 1200), "RGBA", "PNG"),
        ((40, 30), "P", "GIF"),
        ((320, 320), "RGB", "BMP"),
    ],
)
def test_thumbnail_spec(thumb_dir, size, mode, fmt):
    source = thumb_dir / "source"
    Image.new(mode, size).save(source, format=fmt)
    path = media.normalize_thumbnail(str(source), media.thumbnail_path(123))
    with Image.open(path) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"
        assert max(image.size) <= 320
        assert abs(image.width / image.height - size[0] / size[1]) < 0.02
    assert 0 < os.path.getsize(path) < 200_000


def test_thumbnail_noise_below_byte_limit(thumb_dir):
    source = thumb_dir / "noise.png"
    Image.frombytes("RGB", (800, 800), os.urandom(800 * 800 * 3)).save(source)
    dest = media.normalize_thumbnail(str(source), media.thumbnail_path(1))
    assert Path(dest).stat().st_size < 200_000


def test_invalid_thumbnail_keeps_existing_and_cleans_temp(thumb_dir):
    target = Path(media.thumbnail_path(1))
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous")
    source = thumb_dir / "invalid"
    source.write_bytes(b"invalid")
    with pytest.raises(OSError):
        media.normalize_thumbnail(str(source), str(target))
    assert target.read_bytes() == b"previous"
    assert list(target.parent.iterdir()) == [target]


def test_persistent_thumb_migrates_legacy_and_deletes(thumb_dir):
    Image.new("RGB", (640, 480)).save("7.jpg")
    target = media.custom_thumb_path(7)
    assert target == media.thumbnail_path(7)
    assert not Path("7.jpg").exists()
    os.chdir(thumb_dir / "persistent")
    assert media.custom_thumb_path(7) == target
    assert media.remove_custom_thumb(7) == "removed"
    assert media.remove_custom_thumb(7) == "absent"


def test_path_rejects_traversal(thumb_dir):
    with pytest.raises(ValueError):
        media.thumbnail_path("../8")


@pytest.mark.parametrize("success", [True, False])
def test_screenshot_closes_mkstemp_fd_and_cleans(monkeypatch, thumb_dir, success):
    descriptors = []
    original = media.tempfile.mkstemp

    def mkstemp(*args, **kwargs):
        fd, path = original(*args, **kwargs)
        descriptors.append(fd)
        return fd, path

    async def run(*cmd):
        with pytest.raises(OSError):
            os.fstat(descriptors[0])
        assert "scale=320:320:force_original_aspect_ratio=decrease" in cmd
        if success:
            Image.new("RGB", (1920, 1080)).save(cmd[-1])
            return b""
        return None

    monkeypatch.setattr(media.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(media, "_run_process", run)
    output = asyncio.run(media.screenshot("video", 100, str(thumb_dir)))
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)
    if success:
        with Image.open(output) as image:
            assert image.size == (320, 180)
        assert Path(output).stat().st_size < 200_000
    else:
        assert output is None
        assert not list(thumb_dir.glob("*.jpg"))


def test_screenshot_cancel_removes_temporary(monkeypatch, thumb_dir):
    monkeypatch.setattr(media, "_run_process", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(media.screenshot("video", 1, str(thumb_dir)))
    assert not list(thumb_dir.glob("*.jpg"))


def test_screenshot_invalid_output_removed(monkeypatch, thumb_dir):
    monkeypatch.setattr(media, "_run_process", AsyncMock(return_value=b""))
    assert asyncio.run(media.screenshot("video", 1, str(thumb_dir))) is None
    assert not list(thumb_dir.glob("*.jpg"))


def test_real_subprocess_success():
    import sys

    assert asyncio.run(media._run_process(sys.executable, "-c", "print('probe')")) == b"probe\n"


def test_real_subprocess_timeout_reaped(monkeypatch, tmp_path):
    import sys

    spawned = []
    original = media.asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(media, "PROCESS_TIMEOUT", 0.1)
    result = asyncio.run(media._run_process(sys.executable, "-c", "import time; time.sleep(60)"))
    assert result is None
    assert spawned[0].returncode is not None
    # communicate 完成后子进程已回收，父进程没有可 wait 的僵尸进程。
    with pytest.raises(ChildProcessError):
        os.waitpid(spawned[0].pid, os.WNOHANG)


def test_thumbnail_partial_removal_reports_failure(monkeypatch, tmp_path):
    destination = tmp_path / "thumb.jpg"
    destination.write_bytes(b"image")
    monkeypatch.setattr(media, "thumbnail_path", lambda _: str(destination))
    original = media.os.remove

    def remove(path):
        if path == "987.jpg":
            raise PermissionError("read-only")
        return original(path)

    monkeypatch.setattr(media.os, "remove", remove)
    assert media.remove_custom_thumb(987) == "failed"
    assert not destination.exists()
