"""按 UTF-8 字节预算处理文件名，保留扩展名及稳定的冲突后缀。"""

import logging
import os
import re
import time

from tgforward.utils.text import apply_text_rules

logger = logging.getLogger(__name__)
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\'\x00-\x1f]')
MAX_FILENAME_BYTES = 250
NAMED_MEDIA_TYPES = ("video", "audio", "document", "animation")


def _clip(value: str, budget: int) -> str:
    return value.encode("utf-8")[: max(0, budget)].decode("utf-8", errors="ignore")


def _fit_name(stem: str, ext: str, suffix: str = "") -> str:
    # 极端伪扩展名也受预算约束；正常扩展名完整保留。
    ext = _clip(ext, MAX_FILENAME_BYTES - len(suffix.encode()) - 4)
    budget = MAX_FILENAME_BYTES - len((ext + suffix).encode())
    return (_clip(stem, budget).rstrip(" .") or "file") + suffix + ext


def sanitize_filename(name: str) -> str:
    cleaned = _ILLEGAL_CHARS.sub("_", name).strip(" .") or "file"
    return _fit_name(*os.path.splitext(cleaned))


def original_media_name(message) -> str | None:
    for attr in NAMED_MEDIA_TYPES:
        name = getattr(getattr(message, attr, None), "file_name", None)
        if name:
            return name
    return None


def media_filename(message, fallback_ts: float | None = None) -> str:
    name = original_media_name(message)
    if name:
        return sanitize_filename(name)
    stamp = str(time.time() if fallback_ts is None else fallback_ts)
    return stamp + ".jpg" if getattr(message, "photo", None) else stamp


def processed_name(name, delete_words, replacements, rename_tag=""):
    stem, ext = os.path.splitext(sanitize_filename(name))
    stem = apply_text_rules(stem, replacements, delete_words).strip()
    if rename_tag:
        stem = f"{stem} {rename_tag}".strip()
    return sanitize_filename((stem or "file") + ext)


def apply_name_rules(
    file_path: str, delete_words: list[str], replacements: dict[str, str], rename_tag: str = ""
) -> str:
    directory, name = os.path.split(file_path)
    name = processed_name(name, delete_words, replacements, rename_tag)
    destination = os.path.join(directory, name)
    if destination == file_path:
        return file_path
    stem, ext = os.path.splitext(name)
    # os.link 的独占创建保证并发冲突时不覆盖已有文件；同目录不跨设备。
    counter = 0
    while True:
        try:
            os.link(file_path, destination)
            try:
                os.unlink(file_path)
            except OSError:
                os.unlink(destination)
                raise
            return destination
        except FileExistsError:
            counter += 1
            destination = os.path.join(directory, _fit_name(stem, ext, f" ({counter})"))
        except OSError as exc:
            logger.warning("重命名失败 %s -> %s: %s", file_path, destination, exc)
            return file_path
