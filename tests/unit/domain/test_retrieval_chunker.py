"""Paragraph sliding-window chunker boundaries."""

from __future__ import annotations

from itertools import pairwise

from proseforge.domain.retrieval.chunker import (
    MAX_CHUNK_CHARS,
    OVERLAP_CHARS,
    chunk_text,
)


def test_blank_input_returns_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("  \n\n \r\n ") == []


def test_short_text_is_single_chunk():
    text = "第一段。\n\n第二段。"
    assert chunk_text(text) == ["第一段。\n第二段。"]


def test_crlf_and_cr_are_normalized():
    chunks = chunk_text("aaa\r\n\r\nbbb\rccc")
    assert chunks == ["aaa\nbbb\nccc"]


def test_paragraphs_pack_into_windows_with_overlap():
    # 100 paragraphs of 20 chars each -> several <=700 windows; each flush
    # carries trailing paragraphs (<=105 chars) into the next window.
    paragraphs = [f"段落{i:03d}abcdefghijklmno" for i in range(100)]  # 20 chars each
    assert all(len(p) == 20 for p in paragraphs)
    chunks = chunk_text("\n\n".join(paragraphs))
    assert len(chunks) > 3
    for chunk in chunks:
        assert len(chunk) <= MAX_CHUNK_CHARS
    # Overlap: consecutive windows share trailing paragraphs.
    for previous, current in pairwise(chunks):
        prev_units = previous.split("\n")
        curr_units = current.split("\n")
        shared = [unit for unit in curr_units[:6] if unit in set(prev_units)]
        assert shared, "expected carried overlap between consecutive chunks"
        assert sum(len(unit) for unit in shared) <= OVERLAP_CHARS


def test_oversized_paragraph_hard_split_with_overlap():
    paragraph = "字" * 2000
    chunks = chunk_text(paragraph)
    assert len(chunks) >= 3
    for chunk in chunks:
        assert len(chunk) <= MAX_CHUNK_CHARS
    # Step = MAX - OVERLAP: consecutive pieces overlap by ~OVERLAP_CHARS.
    first, second = chunks[0], chunks[1]
    assert first[-OVERLAP_CHARS:] == second[:OVERLAP_CHARS]


def test_window_boundaries_respect_paragraph_integrity():
    paragraphs = ["p" * 300, "q" * 300, "r" * 300]
    chunks = chunk_text("\n\n".join(paragraphs))
    # 300+1+300 = 601 fits; adding the third (300) would exceed 700 -> flush.
    assert chunks[0] == "p" * 300 + "\n" + "q" * 300
    # Second window starts with the overlap tail and ends with the new paragraph.
    assert chunks[-1].endswith("r" * 300)
    for chunk in chunks:
        assert len(chunk) <= MAX_CHUNK_CHARS


def test_custom_max_chars_scales_window_and_overlap():
    # Local engine window: 450 chars, overlap stays proportional at 15% (67).
    overlap = 450 * 15 // 100
    chunks = chunk_text("字" * 2000, max_chars=450)
    assert len(chunks) >= 4
    for chunk in chunks:
        assert len(chunk) <= 450
    first, second = chunks[0], chunks[1]
    assert first[-overlap:] == second[:overlap]


def test_custom_max_chars_packs_paragraphs_tighter():
    paragraphs = ["p" * 200, "q" * 200, "r" * 200]
    chunks = chunk_text("\n\n".join(paragraphs), max_chars=450)
    # 200+1+200 = 401 fits; adding the third (200) would exceed 450 -> flush.
    assert chunks[0] == "p" * 200 + "\n" + "q" * 200
    assert chunks[-1].endswith("r" * 200)


def test_invalid_bounds_raise():
    import pytest

    with pytest.raises(ValueError):
        chunk_text("正文", max_chars=100, min_chars=200)
