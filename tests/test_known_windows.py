"""known_windows 表查询与 capabilities_from_model 的窗口解析路径。"""

from __future__ import annotations

from proseforge.domain.model.capabilities import capabilities_from_model
from proseforge.domain.model.known_windows import lookup_context_window
from proseforge.domain.ports.model_provider import ProviderModel


def test_lookup_exact_match_wins() -> None:
    assert lookup_context_window("kimi", "moonshot-v1-8k") == 8192
    assert lookup_context_window("kimi", "moonshot-v1-128k") == 131072


def test_lookup_prefix_and_substring() -> None:
    assert lookup_context_window("openai", "gpt-4o-mini") == 128000
    assert lookup_context_window("openai", "gpt-4.1-nano") == 1047576
    assert lookup_context_window("openai", "gpt-5") == 400000
    assert lookup_context_window("openai", "gpt-5.5-turbo") == 1050000
    assert lookup_context_window("anthropic", "claude-sonnet-4-20250514") == 200000
    assert lookup_context_window("anthropic", "claude-opus-4-6") == 1000000
    assert lookup_context_window("dashscope", "qwen-long-latest") == 10000000
    assert lookup_context_window("dashscope", "qwen-max-2025-01-25") == 131072
    assert lookup_context_window("dashscope", "qwen-plus") == 131072
    # deepseek chat/reasoner aliases deprecated 2026-07-24, map onto v4-flash (1M)
    assert lookup_context_window("deepseek", "deepseek-chat") == 1048576
    assert lookup_context_window("deepseek", "deepseek-r1") == 1048576
    assert lookup_context_window("kimi", "kimi-k3") == 1048576
    # kimi-latest rule dropped in the 2026-07-26 audit (K2 old line retired);
    # unknown models must fall through to None
    assert lookup_context_window("kimi", "kimi-latest") is None
    assert lookup_context_window("zhipu", "glm-4.6") == 200000


def test_lookup_researched_providers() -> None:
    # Values verified against official provider docs (2026-07).
    assert lookup_context_window("volcengine", "doubao-seed-1-6-250615") == 262144
    assert lookup_context_window("volcengine", "doubao-seed-2-0-lite-260428") == 262144
    assert lookup_context_window("volcengine", "doubao-1-5-pro-32k-250115") == 32768
    assert lookup_context_window("volcengine", "doubao-1-5-vision-pro-32k-250115") == 32768
    assert lookup_context_window("baidu", "ernie-5.1") == 131072
    assert lookup_context_window("baidu", "ernie-4.5-turbo-128k") == 131072
    assert lookup_context_window("baidu", "ernie-4.5-turbo-32k") == 32768
    assert lookup_context_window("baidu", "ernie-x1-turbo-32k") == 32768
    assert lookup_context_window("baidu", "ernie-x1") == 32768
    assert lookup_context_window("baidu", "ernie-speed-128k") == 131072
    # tencent turbos/standard-256k/t1 retired 2026-06-26 → hunyuan- catch-all
    assert lookup_context_window("tencent", "hunyuan-turbos-latest") == 32768
    assert lookup_context_window("tencent", "hunyuan-standard-256k") == 32768
    assert lookup_context_window("tencent", "hunyuan-t1-latest") == 32768
    assert lookup_context_window("mistral", "mistral-large-3") == 262144
    assert lookup_context_window("mistral", "mistral-large-2411") == 131072
    assert lookup_context_window("mistral", "devstral-medium-2507") == 131072
    assert lookup_context_window("cohere", "command-a-03-2025") == 262144
    assert lookup_context_window("cohere", "command-r-plus") == 128000
    assert lookup_context_window("xai", "grok-4.3") == 1000000
    # grok-4 retired 2026-05-15 → grok- catch-all (Grok 4.5 official 500K)
    assert lookup_context_window("xai", "grok-4-0709") == 500000


def test_lookup_rule_order_keeps_specific_patterns_first() -> None:
    # Official 火山方舟 list (re-audited 2026-07-26): 1-5-pro-32k really is a
    # 32K window — the "-32k" suffix rules must win over the "1.5-pro" prefix.
    assert lookup_context_window("volcengine", "doubao-1.5-pro-32k") == 32768
    assert lookup_context_window("volcengine", "doubao-1.5-vision-pro") == 32768
    # gemini-1.0 is retired: its specific rule was dropped in the 2026-07-26
    # audit, so it falls through to the gemini- catch-all (1M).
    assert lookup_context_window("google", "gemini-1.0-pro") == 1048576
    assert lookup_context_window("google", "gemini-2.5-pro") == 1048576


def test_lookup_is_case_insensitive_and_unknown_returns_none() -> None:
    assert lookup_context_window("OpenAI", "GPT-4O") == 128000
    assert lookup_context_window("openai", "some-future-model") is None
    assert lookup_context_window(None, "gpt-4o") is None
    assert lookup_context_window("openai", "") is None


def test_lookup_cross_vendor_fallback_for_plan_proxies() -> None:
    # Plan/proxy endpoints (opencode go 套餐等) register as a generic provider
    # but serve official models of other vendors — resolve the real windows.
    # Scoped openai-gateway rules (2026-08-08 research) carry the gateway's
    # deployed values and take precedence over the cross-vendor fallback.
    assert lookup_context_window("openai", "deepseek-v4-flash") == 1000000
    assert lookup_context_window("openai", "deepseek-v4-pro") == 1000000
    assert lookup_context_window("openai", "glm-5.2") == 1000000
    assert lookup_context_window("openai", "glm-5.1") == 204800
    assert lookup_context_window("openai", "glm-5") == 204800
    assert lookup_context_window("openai", "kimi-k3") == 1048576
    assert lookup_context_window("openai", "kimi-k2.6") == 262144
    assert lookup_context_window("openai", "kimi-k2.7-code") == 262144
    assert lookup_context_window("openai", "minimax-m2.5") == 204800
    assert lookup_context_window("openai", "minimax-m3") == 512000
    assert lookup_context_window("openai", "grok-4.5") == 500000
    assert lookup_context_window("openai", "qwen3.5-plus") == 262144
    assert lookup_context_window("openai", "qwen3.6-plus") == 262144
    assert lookup_context_window("openai", "qwen3.7-max") == 1000000
    assert lookup_context_window("openai", "mimo-v2.5-pro") == 1050000
    assert lookup_context_window("openai", "mimo-v2.5") == 1050000
    assert lookup_context_window("openai", "mimo-v2-omni") == 262144
    assert lookup_context_window("openai", "hy3") == 256000
    assert lookup_context_window("openai", "hy3-preview") == 256000
    # Providers without scoped gateway rules still resolve cross-vendor.
    assert lookup_context_window("ghost", "gpt-4o") == 128000
    assert lookup_context_window("ghost", "deepseek-v4-flash") == 1048576
    # Empty-pattern platform catch-alls (groq/cerebras) must not leak across vendors.
    assert lookup_context_window("openai", "totally-unknown-9000") is None


def test_capabilities_known_window_beats_catalog_value() -> None:
    # Verified known windows beat user-entered catalog values: a manual entry
    # must never shrink a model below its documented window (capabilities.py).
    model = ProviderModel("openai", "gpt-4o", "GPT-4o", {}, context_window=999)
    assert capabilities_from_model(model).context_window == 128000


def test_capabilities_uses_table_when_window_missing() -> None:
    model = ProviderModel("openai", "gpt-4o", "GPT-4o", {})
    assert capabilities_from_model(model).context_window == 128000


def test_capabilities_manual_model_reads_window_from_capabilities_json() -> None:
    model = ProviderModel("custom", "my-model", "My Model", {"context_window": 54321, "manual": True})
    assert capabilities_from_model(model).context_window == 54321


def test_capabilities_unknown_model_keeps_8192_fallback() -> None:
    model = ProviderModel("ghost", "ghost-model", "Ghost", {})
    assert capabilities_from_model(model).context_window == 8192


def test_capabilities_display_uses_real_window_without_product_cap() -> None:
    # The 700K display cap is gone (2026-08-08): the models panel and the
    # usage ring show the real verified window; budgeting caps separately.
    model = ProviderModel("openai", "deepseek-v4-flash", "DSv4 Flash", {})
    assert capabilities_from_model(model).context_window == 1000000
    model = ProviderModel("openai", "hy3", "HY3", {}, context_window=6152)
    assert capabilities_from_model(model).context_window == 256000


def test_context_budget_cap_defaults_and_env_override(monkeypatch) -> None:
    from proseforge.domain.model.capabilities import context_budget_cap

    monkeypatch.delenv("PROSEFORGE_CONTEXT_BUDGET_CAP", raising=False)
    assert context_budget_cap() == 1_048_576
    monkeypatch.setenv("PROSEFORGE_CONTEXT_BUDGET_CAP", "700000")
    assert context_budget_cap() == 700000
    monkeypatch.setenv("PROSEFORGE_CONTEXT_BUDGET_CAP", "junk")
    assert context_budget_cap() == 1_048_576
