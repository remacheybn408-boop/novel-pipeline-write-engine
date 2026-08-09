"""parse_model_json: fence/prose-tolerant model JSON parsing (B4)."""

from __future__ import annotations

import json

import pytest

from proseforge.application.agents.model_json import parse_model_json


def test_plain_json_object():
    assert parse_model_json('{"summary": "ok"}') == {"summary": "ok"}


@pytest.mark.parametrize("text", [
    '```json\n{"summary": "ok"}\n```',
    '```\n{"summary": "ok"}\n```',
    '```JSON {"summary": "ok"} ```',
])
def test_code_fence_is_stripped(text):
    assert parse_model_json(text) == {"summary": "ok"}


def test_leading_prose_before_json():
    text = '好的，以下是评审结果：\n{"summary": "ok", "issues": []}\n希望对你有帮助。'
    assert parse_model_json(text) == {"summary": "ok", "issues": []}


def test_fence_with_leading_prose():
    text = '以下是输出：\n```json\n{"summary": "ok"}\n```'
    assert parse_model_json(text) == {"summary": "ok"}


@pytest.mark.parametrize("text", ["", "   ", "完全不是 JSON 的输出。", "```json\n{broken\n```"])
def test_unparseable_raises_json_decode_error(text):
    with pytest.raises(json.JSONDecodeError):
        parse_model_json(text)


def test_literal_control_chars_inside_strings_tolerated():
    # 长文本模型常在 JSON 字符串值里直接输出换行/制表等控制字符，
    # strict JSON 会拒绝（Invalid control character），应容错解析。
    text = '{"title": "第三章", "content": "第一行\n第二行\t缩进"}'
    assert parse_model_json(text) == {"title": "第三章", "content": "第一行\n第二行\t缩进"}
