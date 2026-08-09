"""artifact_texts: payload text extraction + head-70%/tail-20% elision."""

from __future__ import annotations

import json

from proseforge.application.agents.artifact_texts import (
    elide_middle,
    extract_artifact_text,
)


def test_elide_short_text_untouched():
    assert elide_middle("短正文", 100) == "短正文"
    text = "x" * 100
    assert elide_middle(text, 100) == text  # boundary: exactly at cap


def test_elide_long_text_keeps_head_70_tail_20():
    text = "头" * 7000 + "中" * 2000 + "尾" * 1000  # 10000 chars
    result = elide_middle(text, 1000)
    assert result.startswith("头" * 700)  # head 70% of the cap
    assert result.endswith("尾" * 200)  # tail 20% of the cap
    assert "中段省略 9100 字" in result  # 10000 - 700 - 200 omitted
    assert "中" not in result.replace("中段省略", "")


def test_extract_artifact_text_scene_content():
    assert extract_artifact_text({"title": "t", "content": "正文"}) == "正文"
    # JSON string payloads parse too
    assert extract_artifact_text(json.dumps({"content": "正文"}, ensure_ascii=False)) == "正文"


def test_extract_artifact_text_non_scene_json_digest():
    digest = extract_artifact_text({"chapters": [{"title": "一"}]})
    assert json.loads(digest) == {"chapters": [{"title": "一"}]}


def test_extract_artifact_text_edge_cases():
    assert extract_artifact_text({"content": "   "}) != "   "  # blank content -> digest
    assert extract_artifact_text(None) == ""
    assert extract_artifact_text("plain text") == "plain text"
