"""Telegram 消息 URL：检测候选后统一通过完整 path 校验。"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

MAX_BATCH_COUNT = 10_000
# 候选取完整非空白 token，禁止把非法尾巴前的合法前缀当成一个消息链接。
URL_PATTERN = re.compile(r'https?://[^\s<>"\u3000]+', re.IGNORECASE)
_PRIVATE_RE = re.compile(r"/c/([0-9]+)(?:/([0-9]+))?/([0-9]+)", re.IGNORECASE)
_PUBLIC_RE = re.compile(r"/([A-Za-z0-9_]+)(?:/([0-9]+))?/([0-9]+)")


@dataclass(frozen=True)
class MessageLink:
    chat: str
    message_id: int
    is_private: bool
    comment_id: int | None = None


def _valid_id(value: str) -> bool:
    return value.isascii() and value.isdecimal() and 0 < int(value) < 2**31


def parse_link(url: str) -> MessageLink | None:
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
        return None
    try:
        parts = urlsplit(url)
        if (
            parts.scheme.lower() not in ("http", "https")
            or parts.netloc.lower() not in ("t.me", "www.t.me", "telegram.me", "www.telegram.me")
            or parts.fragment
        ):
            return None
        query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=32)
        comments = query.get("comment")
        if comments is not None and (len(comments) != 1 or not _valid_id(comments[0])):
            return None
        comment_id = int(comments[0]) if comments else None
        match = _PRIVATE_RE.fullmatch(parts.path)
        private = match is not None
        if match is None:
            match = _PUBLIC_RE.fullmatch(parts.path)
        if match is None:
            return None
        chat, topic, mid = match.groups()
        if not _valid_id(mid) or (topic is not None and not _valid_id(topic)):
            return None
        if private:
            if int(chat) <= 0 or len(chat) > 13:
                return None
            chat = f"-100{int(chat)}"
        elif chat.lower() == "c":
            return None
        return MessageLink(chat, int(mid), private, comment_id)
    except (ValueError, OverflowError):
        return None


def find_links(text: str) -> list[str]:
    found, seen = [], set()
    for candidate in URL_PATTERN.findall(text or ""):
        # 只剥离自然语言标点，不删路径、片段或 query 中的非法后缀。
        url = candidate.rstrip(".,!;:，。！；：）)]}")
        ref = parse_link(url)
        if ref is None:
            continue
        key = (ref.chat.lower(), ref.message_id, ref.comment_id)
        if key not in seen:
            seen.add(key)
            found.append(url)
    return found


def parse_batch_count(text: str, url: str) -> int:
    pattern = re.escape(url) + r"(\?[^\s]*)?[ \t]+(\d{1,5})"
    match = re.search(pattern, text)
    return min(MAX_BATCH_COUNT, max(1, int(match.group(2)))) if match else 1
