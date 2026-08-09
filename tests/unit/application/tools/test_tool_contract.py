"""Offline tests for the unified tool fence protocol."""

from __future__ import annotations

from proseforge.application.conversations.tool_contract import (
    build_tool_contract,
    parse_tool_fence,
)
from proseforge.application.tools.orchestrator import (
    canonical_args,
    tool_call_id,
    truncate_result,
)
from proseforge.application.tools.registry import TOOL_REGISTRY


def test_tool_fence_parses_json_info_string():
    text = '前文\n```tool: {"name": "read_page", "args": {"url": "https://a.example/", "mode": "full", "max_length": 4000}}\n```\n后文'
    name, args, match = parse_tool_fence(text)
    assert name == "read_page"
    assert args == {"url": "https://a.example/", "mode": "full", "max_length": 4000}
    assert text[match.start():match.end()].startswith("```tool:")


def test_legacy_search_fence_maps_to_search_web():
    name, args, _ = parse_tool_fence("```search: 今日新闻\n```")
    assert name == "search_web" and args == {"query": "今日新闻"}
    # body fallback when the info string is empty
    name, args, _ = parse_tool_fence("```search:\n查询词\n```")
    assert name == "search_web" and args == {"query": "查询词"}


def test_malformed_json_yields_parse_error_not_crash():
    name, args, match = parse_tool_fence("```tool: {not json}\n```")
    assert name == "" and "parse_error" in args and match is not None


def test_first_unprocessed_fence_wins():
    text = '```search: 第一个\n```\n```tool: {"name": "read_page", "args": {"url": "https://a/"}}\n```'
    name, _, _ = parse_tool_fence(text)
    assert name == "search_web"


def test_no_fence_returns_none():
    assert parse_tool_fence("没有围栏") is None


def test_non_dict_args_becomes_parse_error():
    name, args, _ = parse_tool_fence('```tool: {"name": "x", "args": [1, 2]}\n```')
    assert name == "x" and "parse_error" in args


def test_call_id_deterministic_and_arg_order_insensitive():
    args_a = {"url": "https://a.example/", "mode": "full"}
    args_b = {"mode": "full", "url": "https://a.example/"}
    assert canonical_args(args_a) == canonical_args(args_b)
    assert tool_call_id("m1", "read_page", args_a) == tool_call_id("m1", "read_page", args_b)
    assert tool_call_id("m1", "read_page", args_a) != tool_call_id("m2", "read_page", args_a)


def test_truncate_result_keeps_head_and_tail():
    text = "头" * 100 + "中" * 10000 + "尾" * 100
    out = truncate_result(text, 1000)
    assert len(out) <= 1100
    assert out.startswith("头" * 50)
    assert out.endswith("尾" * 50)
    assert "[truncated:" in out
    assert truncate_result("short", 1000) == "short"


def test_contract_lists_enabled_tools_and_untrusted_warning():
    contract = build_tool_contract(list(TOOL_REGISTRY.values()), 4)
    assert "search_web" in contract and "read_page" in contract
    assert "不可信数据" in contract  # anti-injection declaration
    assert "最多触发 4 次工具调用" in contract
    assert "必须使用工具" in contract
