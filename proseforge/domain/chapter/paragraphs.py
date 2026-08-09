"""段落锚点（第 11 项前置）：正文按空行切段，段序号 + content_hash 锚点。

章节版本写回（repositories/chapter.py append_version）时计算锚点并随版本
落库（chapter_versions.paragraph_anchors，迁移 0049_paragraph_anchors）；
定点改写（application/agents/pinpoint_rewrite.py）与承诺 evidence 引用
失效对账共用同一套切分/哈希，保证锚点可复算、可比对。

切分契约：段落分隔符是一个空行（``\\n\\s*\\n``，全角空格/制表符填充的空行
同样算）。split_paragraphs 同时返回分隔符原文，join_paragraphs 重组后与原
文逐字节一致——定点改写只换段落文本、保留分隔符，未标注段落因此字节级
不变。
"""

from __future__ import annotations

import hashlib
import json
import re

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_WHITESPACE = re.compile(r"\s+")

# evidence/锚点里存的 content_hash 与章节版本同级：sha256 全量 hexdigest。
HASH_LEN = 64


def split_paragraphs(content: str) -> tuple[list[str], list[str]]:
    """正文 → (段落列表, 分隔符列表)；join_paragraphs 重组即还原原文。"""
    paragraphs: list[str] = []
    separators: list[str] = []
    cursor = 0
    for match in _PARAGRAPH_BREAK.finditer(content):
        paragraphs.append(content[cursor : match.start()])
        separators.append(match.group(0))
        cursor = match.end()
    paragraphs.append(content[cursor:])
    return paragraphs, separators


def join_paragraphs(paragraphs: list[str], separators: list[str]) -> str:
    """split_paragraphs 的逆操作：字节级重组（分隔符原样保留）。"""
    parts = [paragraphs[0]] if paragraphs else []
    for separator, paragraph in zip(separators, paragraphs[1:]):
        parts.append(separator)
        parts.append(paragraph)
    return "".join(parts)


def paragraph_id(index: int) -> str:
    """段落锚点 id：p + 4 位序号（同一正文确定性复算）。"""
    return f"p{index:04d}"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_anchors(content: str) -> list[dict[str, object]]:
    """正文 → 段落锚点列表 [{paragraph_id, index, content_hash, start, end, chars}]。

    start/end 是段落在原文中的字符偏移（不含分隔符），供前端/审计定位。
    """
    paragraphs, separators = split_paragraphs(content)
    anchors: list[dict[str, object]] = []
    cursor = 0
    for index, paragraph in enumerate(paragraphs):
        anchors.append({
            "paragraph_id": paragraph_id(index),
            "index": index,
            "content_hash": content_hash(paragraph),
            "start": cursor,
            "end": cursor + len(paragraph),
            "chars": len(paragraph),
        })
        cursor += len(paragraph)
        if index < len(separators):
            cursor += len(separators[index])
    return anchors


def anchors_json(content: str) -> str:
    """build_anchors 的 JSON 串（chapter_versions.paragraph_anchors 落库格式）。"""
    return json.dumps(build_anchors(content), ensure_ascii=False)


def _normalize(text: str) -> str:
    """空白归一：引文定位的兜底比较（模型引文常丢/改换行与空格）。"""
    return _WHITESPACE.sub("", text)


def locate_quote(paragraphs: list[str], quote: str) -> list[int]:
    """引文 → 段落序号列表（升序）。精确子串优先；无命中时按空白归一兜底。

    引文跨段（含空行被切段）时返回所有重叠段；完全找不到返回空列表。
    """
    needle = quote.strip()
    if not needle:
        return []
    hits = [index for index, paragraph in enumerate(paragraphs) if needle in paragraph]
    if hits:
        return hits
    folded = _normalize(needle)
    if not folded:
        return []
    return [index for index, paragraph in enumerate(paragraphs) if folded in _normalize(paragraph)]


def locate_anchor(paragraphs: list[str], quote: str) -> dict[str, object] | None:
    """引文 → 首个命中段的锚点（paragraph_id/content_hash/index）；找不到返回 None。

    承诺 evidence 落锚（{chapter, quote, paragraph_id?, content_hash?}）用：
    写 evidence 时顺手记下段落锚点，改段后按 content_hash 判断引用是否失效。
    """
    hits = locate_quote(paragraphs, quote)
    if not hits:
        return None
    index = hits[0]
    return {"paragraph_id": paragraph_id(index), "index": index, "content_hash": content_hash(paragraphs[index])}
