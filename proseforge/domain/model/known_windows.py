"""Known context windows for common models.

Built-in providers do not report ``context_window`` in ``list_models()``, so the
catalog stores ``None`` and everything degrades to the 8192 fallback. This table
maps well-known ``(provider, model pattern)`` pairs to their published context
windows so capabilities resolution can use real values instead.

Rules are ordered: matching runs exact match first, then prefix, then substring,
each pass scanning the list in order — put more specific patterns before general
ones for the same provider. Only include values that are officially documented;
unknown models must fall through to ``None`` (caller applies its own fallback).

When the scoped lookup misses, a cross-vendor fallback resolves model ids that
name another vendor's official model (plan/proxy endpoints like opencode go are
registered under a generic provider such as "openai" but serve deepseek/glm/
kimi/... models). Official-vendor rules are consulted before aggregator rules;
empty-pattern platform catch-alls never leak across vendors.

Last full audit: 2026-07-26 (7-agent swarm + tavily, official docs/model pages
only; platform-deployed windows, NOT nominal model windows). Retired models are
dropped from the table rather than kept with stale values.
"""

from __future__ import annotations

# (provider, model pattern, context_window)
_RULES: tuple[tuple[str, str, int], ...] = (
    # openai (GPT-5.4/5.5/5.6 officially "1.05M"; 5 = 400K; 4.1 = 1M exact 1,047,576)
    ("openai", "gpt-5.6", 1050000),
    ("openai", "gpt-5.5", 1050000),
    ("openai", "gpt-5.4", 1050000),
    ("openai", "gpt-5", 400000),
    ("openai", "gpt-4.1", 1047576),
    ("openai", "gpt-4o", 128000),
    ("openai", "gpt-4-turbo", 128000),
    ("openai", "o1", 200000),
    ("openai", "o3", 200000),
    ("openai", "o4", 200000),
    ("openai", "gpt-3.5", 16385),
    # OpenAI-compatible gateways (opencode zen/go, one-api style): scoped
    # gateway-served values take precedence over the cross-vendor fallback —
    # the gateway's deployed window wins when it differs from the vendor's
    # nominal one (research: tmp/model_research_2026-08-08.json, 2026-08-08).
    ("openai", "deepseek-v4", 1000000),
    ("openai", "glm-5.2", 1000000),
    ("openai", "glm-5.1", 204800),
    ("openai", "glm-5", 204800),
    ("openai", "kimi-k2", 262144),
    ("openai", "kimi-k3", 1048576),
    ("openai", "qwen3.5-plus", 262144),
    ("openai", "qwen3.6-plus", 262144),
    ("openai", "qwen3.7", 1000000),
    ("openai", "minimax-m2", 204800),
    ("openai", "minimax-m3", 512000),
    ("openai", "mimo-v2.5-pro", 1050000),
    ("openai", "mimo-v2.5", 1050000),
    ("openai", "mimo-v2-omni", 262144),
    ("openai", "mimo-v2-pro", 1048576),
    ("openai", "hy3", 256000),
    ("openai", "grok-4.5", 500000),
    # anthropic (official context-windows page only says "1M" — no exact figure;
    # Fable 5 / Opus 4.6+ / Sonnet 4.6+ / Sonnet 5 / Mythos ship the 1M window)
    ("anthropic", "claude-fable", 1000000),
    ("anthropic", "claude-opus-4-8", 1000000),
    ("anthropic", "claude-opus-4-7", 1000000),
    ("anthropic", "claude-opus-4-6", 1000000),
    ("anthropic", "claude-sonnet-5", 1000000),
    ("anthropic", "claude-sonnet-4-6", 1000000),
    ("anthropic", "claude-mythos", 1000000),
    ("anthropic", "claude-", 200000),
    # google (Gemini 2.5/3.x = 1M input exact 1,048,576; 1.0 long retired)
    ("google", "gemini-", 1048576),
    # deepseek (only v4-flash & v4-pro remain on sale, both 1M; chat/reasoner
    # aliases deprecated 2026-07-24 and map onto v4-flash modes)
    ("deepseek", "deepseek-v4", 1048576),
    ("deepseek", "deepseek-", 1048576),
    # kimi (moonshot; K3 = 1M per official models page + CN FAQ; v1 series
    # sunsets 2026-08-31 but values still correct; K2 old line retired
    # 2026-05-25, K2.6/K2.7 are the current 256K line)
    ("kimi", "moonshot-v1-8k", 8192),
    ("kimi", "moonshot-v1-32k", 32768),
    ("kimi", "moonshot-v1-128k", 131072),
    ("kimi", "kimi-k3", 1048576),
    ("kimi", "kimi-k2.6", 262144),
    ("kimi", "kimi-k2.7", 262144),
    # zhipu (GLM-5.2 = 1M; GLM-5/5.1/5-Turbo/4.6/4.7 = 200K; GLM-4.x = 128K)
    ("zhipu", "glm-5.2", 1048576),
    ("zhipu", "glm-5.1", 200000),
    ("zhipu", "glm-5", 200000),
    ("zhipu", "glm-4.6", 200000),
    ("zhipu", "glm-4.7", 200000),
    ("zhipu", "glm-4", 131072),
    # dashscope (aliyun; qwen-long officially 10M; 3.7/3.6-plus/3.6-flash/
    # 3-coder-plus = 1M deployed; 3.6-max-preview & qwen3-max = 256K tier)
    ("dashscope", "qwen-long", 10000000),
    ("dashscope", "qwen3.7", 1048576),
    ("dashscope", "qwen3.6-plus", 1048576),
    ("dashscope", "qwen3.6-max", 262144),
    ("dashscope", "qwen3.6", 1048576),
    ("dashscope", "qwen3.5", 262144),
    ("dashscope", "qwen3-coder", 1048576),
    ("dashscope", "qwen3-max", 262144),
    ("dashscope", "qwen-max", 131072),
    ("dashscope", "qwen", 131072),
    ("dashscope", "qwq", 131072),
    # volcengine (doubao; per official 火山方舟 model list: seed 2.x = 256K,
    # seed-evolving = 1M, Seed-OSS-36B = 512K; 1.5-pro-32k is really 32K and
    # sunsetting — the "-32k" suffix rules must win over the "1.5-pro" prefix)
    ("volcengine", "doubao-seed-evolving", 1048576),
    ("volcengine", "seed-oss", 524288),
    ("volcengine", "doubao-seed-", 262144),
    ("volcengine", "doubao-1-5-pro-256k", 262144),
    ("volcengine", "doubao-1-5-pro-32k", 32768),
    ("volcengine", "doubao-1-5-lite", 32768),
    ("volcengine", "doubao-1-5-vision", 32768),
    ("volcengine", "doubao-1.5-pro-256k", 262144),
    ("volcengine", "doubao-1.5-pro-32k", 32768),
    ("volcengine", "doubao-1.5-pro", 131072),
    ("volcengine", "doubao-1.5-lite", 32768),
    ("volcengine", "doubao-1.5-vision", 32768),
    ("volcengine", "-32k", 32768),
    ("volcengine", "-128k", 131072),
    # baidu (千帆: ERNIE 5.x = 128K; X1 line retired → X1.1 = 64K is the current
    # deep-reasoning model; x1.1 rule must precede the legacy x1 prefix)
    ("baidu", "-32k", 32768),
    ("baidu", "-128k", 131072),
    ("baidu", "ernie-5", 131072),
    ("baidu", "ernie-4.5-turbo-vl", 131072),
    ("baidu", "ernie-x1.1", 65536),
    ("baidu", "ernie-x1", 32768),
    ("baidu", "ernie-speed", 8192),
    ("baidu", "ernie-", 8192),
    # tencent (turbos/t1/standard-256k/large/lite all retired 2026-06-26;
    # hunyuan-a13b is the current flagship: 224K input + 32K output)
    ("tencent", "hunyuan-a13b", 262144),
    ("tencent", "hunyuan-", 32768),
    # minimax (M1/M3 = 1M nominal; M2.x series = 204.8K; abab line delisted.
    # Catch-all stays at the conservative M2 value.)
    ("minimax", "minimax-m1", 1000000),
    ("minimax", "minimax-m3", 1000000),
    ("minimax", "minimax-m2", 204800),
    ("minimax", "minimax-", 204800),
    # xai (Grok 4.5 = 500K official; 4.3/4.20 = exactly 1,000,000 official;
    # grok-3/grok-4 retired 2026-05-15 and redirect to 4.3)
    ("xai", "grok-4.5", 500000),
    ("xai", "grok-4.3", 1000000),
    ("xai", "grok-4.20", 1000000),
    ("xai", "grok-", 500000),
    # mistral (official known-limitations page = deployed windows; Large 3 /
    # Medium 3.5 / Small 4 / Devstral 2 = 256K; Codestral is really 128K;
    # Medium 3.x & Large 2 = 128K)
    ("mistral", "mistral-large-3", 262144),
    ("mistral", "mistral-medium-3.5", 262144),
    ("mistral", "mistral-medium", 131072),
    ("mistral", "mistral-small-4", 262144),
    ("mistral", "devstral-medium", 131072),
    ("mistral", "devstral-small", 131072),
    ("mistral", "devstral", 262144),
    ("mistral", "codestral", 131072),
    ("mistral", "mistral-large", 131072),
    ("mistral", "mistral-", 32768),
    # cohere (Command A = 256K, R family = 128K)
    ("cohere", "command-a", 262144),
    ("cohere", "command-r", 128000),
    # agnes (Sapiens AI; verified 2026-07-26 against the official gateway
    # catalog + wiki: 2.5 Pro Alpha = 1M; 2.5 Flash = 512K (gray release);
    # 2.0 Flash rolled back 512K→256K in 2026-06 — the wiki still shows the
    # stale 512K, the gateway MODEL_CATALOG + third-party deploys agree on 256K)
    ("agnes", "agnes-2.5-pro", 1048576),
    ("agnes", "agnes-2.5-flash", 524288),
    ("agnes", "agnes-2.0-flash", 262144),
    # ===== 2026-07-26 re-audit (7-agent swarm research, official sources;
    # platform-deployed windows, NOT nominal model windows) =====
    # baichuan (official price page: every current API model = 32K except the
    # 128K variant; the 192K variant is delisted)
    ("baichuan", "baichuan3-turbo-128k", 131072),
    ("baichuan", "baichuan", 32768),
    # iflytek (official xfyun.cn spark docs: Lite/Pro/Max = 8K; Ultra = 32K;
    # spark-x (X2) = 64K INPUT window (128K output) — table keeps the input
    # figure because it drives prompt budgeting)
    ("iflytek", "pro-128k", 131072),
    ("iflytek", "max-32k", 32768),
    ("iflytek", "spark-x", 65536),
    ("iflytek", "4.0ultra", 32768),
    ("iflytek", "generalv3", 8192),
    ("iflytek", "lite", 8192),
    ("iflytek", "spark", 8192),
    # sensenova (current platform sells only 6.7-flash-lite 256K + hosted dsv4)
    ("sensenova", "sensenova-", 262144),
    ("sensenova", "deepseek-v4", 1048576),
    # stepfun (step-3 retired 2026-07-08; step-3.5-flash/step-3.7-flash = 256K
    # and match the step-3 prefix)
    ("stepfun", "step-1o-turbo-vision", 32768),
    ("stepfun", "step-3", 262144),
    # yi (platform shrunk to two models, both 16K)
    ("yi", "yi-lightning", 16384),
    ("yi", "yi-vision", 16384),
    # perplexity (sonar-pro = 200K; all other current sonar models = 128K)
    ("perplexity", "sonar-pro", 200000),
    ("perplexity", "sonar", 128000),
    # groq (entire production chat lineup = 131072 per official models table;
    # only guard/TTS models differ; empty pattern is an intentional catch-all)
    ("groq", "prompt-guard", 512),
    ("groq", "orpheus", 4000),
    ("groq", "", 131072),
    # cerebras (public catalog shrank to 3 models; zai-glm-4.7 is tiered
    # 64K free / 131K paid — table takes the free-tier-safe value)
    ("cerebras", "zai-glm", 65536),
    ("cerebras", "", 131072),
    # sambanova (official docs label "XXk tokens"; binary conversions cross-checked
    # against the official community post and independent measurements)
    ("sambanova", "deepseek-v3.2", 32768),
    ("sambanova", "minimax-m2.7", 196608),
    ("sambanova", "deepseek-v3.1", 131072),
    ("sambanova", "llama-3.3-70b", 131072),
    ("sambanova", "gpt-oss", 131072),
    ("sambanova", "gemma-4", 131072),
    # deepinfra (per-model pages, deployed values)
    ("deepinfra", "deepseek-v4", 1048576),
    ("deepinfra", "deepseek-v3.2", 163840),
    ("deepinfra", "qwen3.5-397b", 262144),
    ("deepinfra", "qwen3.6", 262144),
    ("deepinfra", "qwen3-max", 256000),
    ("deepinfra", "kimi-k2.6", 262144),
    ("deepinfra", "kimi-k2.7", 262144),
    ("deepinfra", "glm-5.2", 1048576),
    ("deepinfra", "glm-5", 202752),
    ("deepinfra", "mimo", 1048576),
    ("deepinfra", "gemma-4", 262144),
    ("deepinfra", "nemotron-3-ultra", 262144),
    ("deepinfra", "llama-4-maverick", 1048576),
    ("deepinfra", "llama-4-scout", 327680),
    ("deepinfra", "llama-3.1", 131072),
    ("deepinfra", "gpt-oss", 131072),
    # fireworks (official pages only publish rounded-down approximations;
    # glm-5.2 & deepseek-v4-pro display as "1040k"; kimi-k2p6/k2p5 = 256K)
    ("fireworks", "glm-5.2", 1040000),
    ("fireworks", "glm-5", 202752),
    ("fireworks", "glm-4p6", 202752),
    ("fireworks", "glm-4p7", 202752),
    ("fireworks", "glm-4p5", 131072),
    ("fireworks", "kimi-k2-thinking", 262144),
    ("fireworks", "kimi-k2p", 262144),
    ("fireworks", "kimi-k2-instruct-0905", 262144),
    ("fireworks", "kimi-k2-instruct", 131072),
    ("fireworks", "deepseek-v4", 1040000),
    ("fireworks", "deepseek-v3p2", 163840),
    ("fireworks", "deepseek-v3p1", 163840),
    ("fireworks", "deepseek-r1", 163840),
    ("fireworks", "deepseek-v3", 131072),
    ("fireworks", "qwen3", 262144),
    ("fireworks", "llama-v3p3-70b", 131072),
    ("fireworks", "gpt-oss", 131072),
    ("fireworks", "minimax-m2", 196608),
    ("fireworks", "qwq-32b", 131072),
    # together (official serverless-models table; all entries CONFIRMED 2026-07-26)
    ("together", "qwen3.7", 1000000),
    ("together", "qwen3.6-plus", 1000000),
    ("together", "deepseek-v4-pro", 512000),
    ("together", "minimax-m3", 524288),
    ("together", "minimax-m2.7", 202752),
    ("together", "kimi-k2.7", 262144),
    ("together", "kimi-k2.6", 262144),
    ("together", "glm-5.2", 262144),
    ("together", "nemotron-3-ultra", 512300),
    ("together", "gpt-oss", 128000),
    ("together", "llama-3.3-70b", 131072),
    ("together", "cogito-v2-1", 163840),
    ("together", "gemma-4-31b", 262144),
    ("together", "inkling", 524288),
    # siliconflow (official .cn model cards track the main site; m2.5 is cut to
    # 192K on both sites; glm-4.6 officially Deprecated and dropped)
    ("siliconflow", "glm-5.2", 1048576),
    ("siliconflow", "glm-5.1", 202752),
    ("siliconflow", "glm-4.7", 204800),
    ("siliconflow", "deepseek-v4", 1048576),
    ("siliconflow", "deepseek-v3.2", 163840),
    ("siliconflow", "deepseek-v3.1", 163840),
    ("siliconflow", "deepseek-r1-distill", 131072),
    ("siliconflow", "deepseek-r1", 163840),
    ("siliconflow", "deepseek-v3", 131072),
    ("siliconflow", "kimi-k2", 262144),
    ("siliconflow", "minimax-m3", 1048576),
    ("siliconflow", "minimax-m2.5", 196608),
    ("siliconflow", "longcat", 1048576),
    ("siliconflow", "qwen3.5", 262144),
    ("siliconflow", "qwen3.6", 262144),
    ("siliconflow", "qwen3-235b", 262144),
    ("siliconflow", "qwen3", 131072),
    # novita (official model library publishes token-exact values)
    ("novita", "glm-5.2", 1048576),
    ("novita", "glm-5.1", 204800),
    ("novita", "glm-5", 202800),
    ("novita", "glm-4.7", 204800),
    ("novita", "glm-4.6v", 131072),
    ("novita", "glm-4.6", 204800),
    ("novita", "deepseek-v4", 1048576),
    ("novita", "deepseek-v3.2", 163840),
    ("novita", "deepseek-r1-0528", 163840),
    ("novita", "deepseek-r1-turbo", 64000),
    ("novita", "deepseek-v3.1", 131072),
    ("novita", "qwen3-max", 262144),
    ("novita", "qwen3-coder", 262144),
    ("novita", "qwen3-next", 131072),
    ("novita", "qwen3-235b", 40960),
    ("novita", "qwen2.5-72b", 32000),
    ("novita", "kimi-k3", 1048576),
    ("novita", "kimi-k2", 262144),
    ("novita", "minimax-m1", 1000000),
    ("novita", "minimax-m2", 204800),
    ("novita", "llama-4-maverick", 1048576),
    ("novita", "llama-4-scout", 131072),
    ("novita", "llama-3.3", 131072),
    ("novita", "gemma-3-27b", 98304),
    # openrouter (official /api/v1/models context_length + model pages)
    ("openrouter", "gpt-5.6", 1050000),
    ("openrouter", "claude-sonnet-5", 1000000),
    ("openrouter", "claude-opus-5", 1000000),
    ("openrouter", "claude-opus-4.8", 1000000),
    ("openrouter", "claude-fable-5", 1000000),
    ("openrouter", "gemini-", 1048576),
    ("openrouter", "qwen3.7", 1000000),
    ("openrouter", "deepseek-v4", 1048576),
    ("openrouter", "deepseek-v3.2", 163840),
    ("openrouter", "llama-4", 1048576),
    ("openrouter", "llama-3.3", 131072),
    ("openrouter", "grok-4.5", 500000),
    ("openrouter", "kimi-k3", 1048576),
    ("openrouter", "glm-5.2", 1048576),
)


def lookup_context_window(provider: str | None, model_id: str | None) -> int | None:
    """Return the known context window for a model, or ``None`` if unknown."""
    if not provider or not model_id:
        return None
    provider_key = provider.lower()
    model_key = model_id.lower()
    window = _lookup_scoped(provider_key, model_key)
    if window is not None:
        return window
    # Cross-vendor fallback: plan/proxy endpoints (opencode go, one-api and
    # similar) are registered as a single generic provider (e.g. "openai")
    # but serve other vendors' official models. A model id that names a
    # known vendor's model (deepseek-v4-flash, glm-5.2, kimi-k3, …) should
    # resolve to that vendor's documented window instead of degrading to
    # the 8192 fallback. Official-vendor rules are consulted before
    # aggregator/platform rules so the vendor's own window wins on conflicts
    # (e.g. glm-5.2 = 1M official vs 262144 on together).
    return _lookup_cross_vendor(model_key)


def _lookup_scoped(provider_key: str, model_key: str, *, include_catchall: bool = True) -> int | None:
    candidates = [
        (pattern, window)
        for rule_provider, pattern, window in _RULES
        if rule_provider == provider_key and (include_catchall or pattern)
    ]
    for pattern, window in candidates:
        if model_key == pattern:
            return window
    # Single ordered pass for prefix/substring so list order keeps precedence
    # (e.g. "doubao-1.5-pro-32k" hits "-32k" before "doubao-1.5").
    for pattern, window in candidates:
        if model_key.startswith(pattern) or (pattern and pattern in model_key):
            return window
    return None


# Lookup priority for the cross-vendor fallback: official vendors first (in
# table order of appearance), aggregator/proxy platforms last.
_VENDOR_PRIORITY: tuple[str, ...] = (
    "deepseek", "zhipu", "kimi", "dashscope", "volcengine", "anthropic",
    "google", "xai", "minimax", "tencent", "baidu", "mistral", "cohere",
    "agnes", "baichuan", "iflytek", "sensenova", "stepfun", "yi",
    "perplexity", "openai",
)


def _lookup_cross_vendor(model_key: str) -> int | None:
    # Skip bare/ultra-short model ids — prefix/substring matching on them
    # would produce false positives (e.g. "o1" inside unrelated ids).
    if len(model_key) < 4:
        return None
    ordered_providers = list(_VENDOR_PRIORITY) + sorted(
        {rule_provider for rule_provider, _pattern, _window in _RULES} - set(_VENDOR_PRIORITY)
    )
    for provider_key in ordered_providers:
        # Empty-pattern catch-alls (groq/cerebras) are platform-specific and
        # must never leak across vendors; non-empty scoped rules only.
        window = _lookup_scoped(provider_key, model_key, include_catchall=False)
        if window is not None:
            return window
    return None
