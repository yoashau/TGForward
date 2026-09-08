"""视频探测与持久封面；外部工具失败时省略元数据/截图，不中断上传。"""

import asyncio
import contextlib
import io
import json
import logging
import math
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

from tgforward.config import DATA_DIR

logger = logging.getLogger(__name__)
THUMB_MAX_BYTES = 200_000
PROCESS_TIMEOUT = 30


def thumbnail_path(key) -> str:
    """唯一的封面写入路径；只接受整数用户标识，杜绝目录穿越。"""
    return str(Path(DATA_DIR) / "thumbs" / f"{int(key)}.jpg")


def normalize_thumbnail(source: str, destination: str) -> str:
    """首帧、EXIF 旋转、JPEG、最大 320px、严格小于 200KB；原子替换。"""
    with Image.open(source) as opened:
        frame = ImageOps.exif_transpose(opened)
        frame.thumbnail((320, 320), Image.Resampling.LANCZOS)
        rgb = Image.new("RGB", frame.size, "white")
        rgba = frame.convert("RGBA")
        rgb.paste(rgba, mask=rgba.getchannel("A"))
        data = None
        for quality in (90, 80, 65, 50, 35, 20):
            output = io.BytesIO()
            rgb.save(output, "JPEG", quality=quality, optimize=True)
            if output.tell() < THUMB_MAX_BYTES:
                data = output.getvalue()
                break
        if data is None:
            raise ValueError("缩略图压缩后仍超出大小限制")
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(suffix=".jpg", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(temporary, target)
    finally:
        _silent_remove(temporary)
    return str(target)


def custom_thumb_path(key) -> str | None:
    path = thumbnail_path(key)
    if os.path.isfile(path):
        return path
    # 工作目录中的封面按需迁入持久目录，并完成规格校验。
    legacy = f"{int(key)}.jpg"
    if os.path.isfile(legacy):
        try:
            normalize_thumbnail(legacy, path)
            _silent_remove(legacy)
            return path
        except (OSError, ValueError) as exc:
            logger.warning("迁移封面失败 user=%s type=%s", key, type(exc).__name__)
    return None


def remove_custom_thumb(key) -> str:
    removed = failed = False
    for path in {thumbnail_path(key), f"{int(key)}.jpg"}:
        try:
            os.remove(path)
            removed = True
        except FileNotFoundError:
            pass
        except OSError as exc:
            failed = True
            logger.warning("删除封面失败 %s: %s", path, exc)
    return "failed" if failed else "removed" if removed else "absent"


async def _run_process(*cmd) -> bytes | None:
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as exc:
        logger.warning("%s 启动失败：%s", cmd[0], exc)
        return None
    # shield 保留唯一 communicate 调用；超时或取消后 kill 并排空管道、回收进程。
    communicate = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(
            asyncio.shield(communicate), timeout=PROCESS_TIMEOUT
        )
    except (TimeoutError, asyncio.CancelledError) as exc:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await asyncio.shield(communicate)
        if isinstance(exc, asyncio.CancelledError):
            raise
        logger.warning("%s 超时", cmd[0])
        return None
    if process.returncode:
        logger.warning("%s 失败：%s", cmd[0], stderr.decode(errors="replace")[:200])
        return None
    return stdout


async def get_video_metadata(file_path: str) -> dict | None:
    stdout = await _run_process(
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    )
    if stdout is None:
        return None
    try:
        info = json.loads(stdout)
        stream = next(s for s in info.get("streams", []) if s.get("codec_type") == "video")
        result = {}
        for field in ("width", "height"):
            value = int(stream.get(field) or 0)
            if value > 0:
                result[field] = value
        duration = float(info.get("format", {}).get("duration") or stream.get("duration") or 0)
        if math.isfinite(duration) and duration > 0:
            result["duration"] = max(1, round(duration))
        return result or None
    except (ValueError, TypeError, OverflowError, AttributeError, StopIteration) as exc:
        logger.warning("解析视频元数据失败 %s: %s", file_path, exc)
        return None


async def screenshot(
    video_path: str, duration: int | None, out_dir: str | None = None
) -> str | None:
    output_file = None
    keep = False
    try:
        fd, output_file = tempfile.mkstemp(suffix=".jpg", dir=out_dir)
        os.close(fd)
        stdout = await _run_process(
            "ffmpeg",
            "-ss",
            str(max(0, (duration or 0) // 2)),
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            "scale=320:320:force_original_aspect_ratio=decrease",
            "-q:v",
            "3",
            "-y",
            output_file,
        )
        if stdout is None:
            return None
        normalize_thumbnail(output_file, output_file)
        keep = True
        return output_file
    except (OSError, ValueError) as exc:
        logger.warning("截图失败：%s", exc)
        return None
    finally:
        if not keep:
            _silent_remove(output_file)


def _silent_remove(path: str | None) -> None:
    if path:
        with contextlib.suppress(OSError):
            os.remove(path)
