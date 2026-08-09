"""Paragraph sliding-window chunker for narrative RAG.

Splits chapter text on paragraph boundaries (blank-line or newline
separated, CR/LF safe — Chinese and English prose alike), greedily packs
paragraphs into windows of 350-700 characters, and carries a ~15% tail
(<=105 chars of trailing paragraphs) into the next window as overlap.
A single paragraph longer than the window is hard-split with the same
overlap step. Bounds are soft: a window never starts below the minimum
unless the text itself is short, and hard-split pieces never exceed the
maximum. Character counts are a deliberate token proxy — safe for CJK.

``chunk_text`` accepts optional window bounds (the local embedding engine
chunks tighter than the API default because 512-token models fit ~450
CJK chars); overlap always stays at 15% of the window.
"""

from __future__ import annotations

MIN_CHUNK_CHARS = 350
MAX_CHUNK_CHARS = 700
OVERLAP_CHARS = MAX_CHUNK_CHARS * 15 // 100  # 105


def _split_paragraphs(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in normalized.split("\n") if line.strip()]


def _hard_split(paragraph: str, max_chars: int, overlap_chars: int) -> list[str]:
    step = max_chars - overlap_chars
    return [paragraph[i : i + max_chars] for i in range(0, len(paragraph), step)]


def _overlap_tail(window: list[str], overlap_chars: int) -> list[str]:
    tail: list[str] = []
    total = 0
    for unit in reversed(window):
        if total + len(unit) > overlap_chars:
            break
        tail.insert(0, unit)
        total += len(unit)
    return tail


def chunk_text(
    text: str,
    *,
    max_chars: int = MAX_CHUNK_CHARS,
    min_chars: int = MIN_CHUNK_CHARS,
) -> list[str]:
    """Split text into overlapping chunks; returns [] for blank input."""
    if not 0 < min_chars <= max_chars:
        raise ValueError(f"invalid chunk bounds: min_chars={min_chars}, max_chars={max_chars}")
    overlap_chars = max_chars * 15 // 100
    units: list[str] = []
    for paragraph in _split_paragraphs(text):
        if len(paragraph) > max_chars:
            units.extend(_hard_split(paragraph, max_chars, overlap_chars))
        else:
            units.append(paragraph)
    if not units:
        return []

    chunks: list[str] = []
    current: list[str] = []

    def window_len(window: list[str]) -> int:
        return sum(len(unit) for unit in window) + max(0, len(window) - 1)

    for unit in units:
        if current and window_len(current) + 1 + len(unit) > max_chars:
            chunks.append("\n".join(current))
            current = _overlap_tail(current, overlap_chars)
        current.append(unit)
    if current:
        chunks.append("\n".join(current))
    return chunks
