"""纯文本规则与 Telegram UTF-16 实体操作；不生成/重新解析 Markdown。"""

from bisect import bisect_right
from copy import copy


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class RichText(str):
    """保持字符串调用契约，同时携带独立的 Telegram MessageEntity 列表。"""

    def __new__(cls, text="", entities=None):
        value = super().__new__(cls, text)
        value.entities = [copy(entity) for entity in (entities or [])]
        return value


def message_text(message, field="text") -> RichText:
    value = getattr(message, field, None)
    plain = str(value) if value is not None else ""
    entities = getattr(message, "entities" if field == "text" else "caption_entities", None)
    return RichText(plain, entities)


def _replace(text, old, new):
    if not old or old == new:
        return text
    cursor = 0
    entities = [copy(e) for e in getattr(text, "entities", [])]
    result = str(text)
    while (start := result.find(old, cursor)) >= 0:
        end = start + len(old)
        left, right = utf16_len(result[:start]), utf16_len(result[:end])
        size = utf16_len(new)
        delta = size - (right - left)
        updated = []
        for entity in entities:
            a, b = entity.offset, entity.offset + entity.length
            if b <= left:
                pass
            elif a >= right:
                entity.offset += delta
            else:
                kind = str(getattr(entity.type, "name", entity.type)).lower()
                # 内容型实体的标签依赖精确字符；编辑后放弃旧标签，不伪造 URL/emoji。
                if kind not in (
                    "bold",
                    "italic",
                    "underline",
                    "strikethrough",
                    "spoiler",
                    "code",
                    "pre",
                    "text_link",
                    "blockquote",
                    "expandable_blockquote",
                ):
                    continue
                if a <= left and b >= right:
                    b += delta  # 替换片段完全在实体中，继承该实体。
                elif left <= a and b <= right:
                    continue  # 被更大替换整体覆盖的局部格式不扩张到新文本。
                elif a < left:
                    b = left  # 跨多个相邻实体的替换不产生交叉重叠。
                else:
                    a, b = left + size, b + delta
                entity.offset, entity.length = a, b - a
                if entity.length <= 0:
                    continue
            updated.append(entity)
        entities = updated
        result = result[:start] + new + result[end:]
        cursor = start + len(new)
    return RichText(result, entities) if isinstance(text, RichText) else result


def apply_text_rules(
    text: str | None, replacements: dict[str, str] | None, delete_words: list[str] | None
) -> str:
    processed = text if text is not None else ""
    for old, new in (replacements or {}).items():
        processed = _replace(processed, old, new)
    for word in delete_words or []:
        processed = _replace(processed, word, "")
    return processed


def append_text(text: str, suffix: str) -> RichText:
    """用户文案视为字面文本；实体只继承原正文，不解释用户 Markdown 控制字符。"""
    return RichText(
        str(text) + ("\n\n" if text and suffix else "") + suffix, getattr(text, "entities", [])
    )


def _slice(text, start, end):
    left, right = utf16_len(text[:start]), utf16_len(text[:end])
    entities = []
    for entity in getattr(text, "entities", []):
        a, b = max(left, entity.offset), min(right, entity.offset + entity.length)
        if a < b:
            clone = copy(entity)
            clone.offset, clone.length = a - left, b - a
            entities.append(clone)
    return RichText(text[start:end], entities)


def split_text(text: str, limit: int = 4096) -> list[str]:
    """按 UTF-16 预算分段，保留全部空白；跨段实体裁剪并重新定位。

    能装入单段的实体优先整体移动；超长 pre/bold/link 等实体在各段重新建立，
    不携带跨段 Markdown 标记，不在 surrogate pair 中间截断。
    """
    if limit < 2:
        raise ValueError("文本分段 limit 至少为 2")
    if not text:
        return [RichText("", getattr(text, "entities", []))] if isinstance(text, RichText) else [""]
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + utf16_len(char))
    chunks, start = [], 0
    while start < len(text):
        end = bisect_right(offsets, offsets[start] + limit) - 1
        if end < len(text):
            for separator in ("\n", " "):
                boundary = text.rfind(separator, start, end) + 1
                if boundary > start and offsets[boundary] - offsets[start] >= limit // 2:
                    end = boundary
                    break
            # 反复向前移动，处理嵌套实体；已在段首开始的超长实体按段裁剪。
            while True:
                candidates = [
                    e.offset
                    for e in getattr(text, "entities", [])
                    if offsets[start] < e.offset < offsets[end] < e.offset + e.length
                    and e.length <= limit
                ]
                if not candidates:
                    break
                end = bisect_right(offsets, min(candidates)) - 1
        chunk = _slice(text, start, end)
        chunks.append(chunk if isinstance(text, RichText) else str(chunk))
        start = end
    return chunks


def truncate_text(text: str, limit: int) -> RichText:
    if utf16_len(text) <= limit:
        return RichText(text, getattr(text, "entities", []))
    count, end = 0, 0
    for i, char in enumerate(text):
        if count + utf16_len(char) > limit - 1:
            break
        count += utf16_len(char)
        end = i + 1
    part = _slice(text, 0, end)
    return RichText(str(part) + "…", part.entities)


def entity_kwargs(text, *, caption=False):
    from pyrogram.enums import ParseMode

    return {
        "parse_mode": ParseMode.DISABLED,
        "caption_entities" if caption else "entities": wire_entities(text),
    }


def wire_entities(text):
    """实体末尾不包含空白；仅修正实体范围，不改动任何正文字符。"""
    plain = str(text or "")
    encoded = plain.encode("utf-16-le")
    result = []
    for entity in getattr(text, "entities", []):
        a, b = entity.offset, entity.offset + entity.length
        if not 0 <= a < b <= len(encoded) // 2:
            continue
        try:
            span = encoded[a * 2 : b * 2].decode("utf-16-le").rstrip()
        except UnicodeDecodeError:
            continue
        if span:
            clone = copy(entity)
            clone.length = utf16_len(span)
            result.append(clone)
    return result
