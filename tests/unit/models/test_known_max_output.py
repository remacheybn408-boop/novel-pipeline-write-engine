"""known_max_output 表：openai 网关（opencode zen/go）作用域规则。"""

from __future__ import annotations

from proseforge.domain.model.known_max_output import lookup_max_output


def test_openai_gateway_scoped_rules() -> None:
    # Values from the 2026-08-08 swarm research (tmp/model_research_2026-08-08.json).
    assert lookup_max_output("openai", "deepseek-v4-flash") == 384000
    assert lookup_max_output("openai", "deepseek-v4-pro") == 384000
    assert lookup_max_output("openai", "glm-5.2") == 131072
    assert lookup_max_output("openai", "glm-5.1") == 131072
    assert lookup_max_output("openai", "kimi-k2.7-code") == 262144
    assert lookup_max_output("openai", "kimi-k2.6") == 65536
    assert lookup_max_output("openai", "kimi-k3") == 131072
    assert lookup_max_output("openai", "qwen3.7-max") == 131072
    assert lookup_max_output("openai", "qwen3.6-plus") == 65536
    assert lookup_max_output("openai", "minimax-m3") == 128000
    assert lookup_max_output("openai", "minimax-m2.5") == 131072
    assert lookup_max_output("openai", "mimo-v2-omni") == 64000
    assert lookup_max_output("openai", "mimo-v2.5-pro") == 131072
    assert lookup_max_output("openai", "hy3") == 64000


def test_openai_grok_45_deliberately_unlisted() -> None:
    # max_output=500K equals the context window — a backfilled placeholder,
    # not a verified limit; falls through to the caller's fallback.
    assert lookup_max_output("openai", "grok-4.5") is None


def test_openai_gpt_56_luna_covered_by_gpt5_prefix() -> None:
    assert lookup_max_output("openai", "gpt-5.6-luna") == 128000
