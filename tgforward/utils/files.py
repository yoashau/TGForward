"""按 UTF-8 字节预算处理文件名，保留扩展名、标签和冲突后缀。"""

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
    if budget <= 0 or not value:
        return ""
    return value.encode("utf-8")[:budget].decode("utf-8", errors="ignore")


def _clip_end(value: str, budget: int) -> str:
    """保留后缀尾部，使冲突序号在预算不够时仍然可区分。"""
    if budget <= 0 or not value:
        return ""
    raw = value.encode("utf-8")
    if len(raw) <= budget:
        return value
    return raw[-budget:].decode("utf-8", errors="ignore")


def _fit_name(stem: str, ext: str, suffix: str = "") -> str:
    # 扩展名和标签/冲突后缀优先于正文；极端伪扩展名也受预算约束。
    ext = _clip(ext, MAX_FILENAME_BYTES - 4)
    suffix_room = MAX_FILENAME_BYTES - len(ext.encode()) - 4
    suffix = _clip_end(suffix, suffix_room)
    budget = MAX_FILENAME_BYTES - len((suffix + ext).encode())
    head = _clip(stem, budget).rstrip(" .") or "file"
    name = head + suffix + ext
    if len(name.encode()) > MAX_FILENAME_BYTES:
        name = _clip(name, MAX_FILENAME_BYTES).rstrip(" .") or "file"
    return name


def _clean(value: str) -> str:
    return _ILLEGAL_CHARS.sub("_", value).strip(" .")


def sanitize_filename(name: str) -> str:
    cleaned = _clean(name) or "file"
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


def _ruled_parts(name, delete_words, replacements, rename_tag=""):
    cleaned = _clean(str(name)) or "file"
    stem, ext = os.path.splitext(cleaned)
    stem = _clean(str(apply_text_rules(stem, replacements, delete_words)))
    tag = _clean(str(rename_tag or ""))
    return stem or "file", ext, tag


def _suffix(tag: str, counter: int = 0) -> str:
    suffix = f" {tag}" if tag else ""
    if counter:
        suffix += f" ({counter})"
    return suffix


def processed_name(name, delete_words, replacements, rename_tag=""):
    stem, ext, tag = _ruled_parts(name, delete_words, replacements, rename_tag)
    return _fit_name(stem, ext, _suffix(tag))


def apply_name_rules(
    file_path: str, delete_words: list[str], replacements: dict[str, str], rename_tag: str = ""
) -> str:
    directory, name = os.path.split(file_path)
    stem, ext, tag = _ruled_parts(name, delete_words, replacements, rename_tag)

    def render(counter: int = 0) -> str:
        return _fit_name(stem, ext, _suffix(tag, counter))

    destination = os.path.join(directory, render())
    if destination == file_path:
        return file_path
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
            nxt = os.path.join(directory, render(counter))
            if nxt == destination:
                logger.warning("无法生成不冲突的文件名 %s", destination)
                return file_path
            destination = nxt
        except OSError as exc:
            logger.warning("重命名失败 %s -> %s: %s", file_path, destination, exc)
            return file_path
