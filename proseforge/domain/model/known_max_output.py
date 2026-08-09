"""Known max output token limits for verified models.

Mirrors known_windows.py / known_reasoning.py: ordered ``(provider, model
pattern, limit)`` rules matched exact first, then prefix/substring in list
order — put more specific patterns before general ones for the same provider.
Only verified values are listed; unknown models fall through to ``None`` and
the caller applies its own fallback (8192).

Verification notes (2026-07):
- deepseek-v4: empirically verified — direct API call with max_tokens=64000
  returned 200.
- google gemini-: strong lead, not first-hand — the official site was
  unreachable, but multiple independent sources agree and Oracle OCI's
  official docs corroborate 65536 for the 2.5 family. Recheck against
  Google's docs when reachable.
- kimi-k3: official default tier; the hard cap is 1M minus prompt tokens.
- kimi-k2.6/k2.5: no fixed official value (= 256K minus prompt); a
  conservative 64K tier is used.
- minimax-m3: recommended value; hard cap 512K. minimax-m2: recommended
  value; hard cap 200K.
- anthropic claude-: fable-5 / opus-4-8 / sonnet-5 verified at 128K; the
  4.5-4.7 generation was not individually verified but belongs to the same
  family, so the generic rule covers it.
- fireworks: no official per-model limit (docs default 2048 is
  unrealistically low); a safe mid-tier 16384 is used.
- deepinfra: platform-wide hard cap, applied as an empty-pattern catch-all
  (same precedent as openrouter in known_reasoning).

Deliberately NOT listed (fall through to the caller's 8192 fallback):
- mistral (mistral-/codestral): officially no independent output limit —
  output budget equals context minus prompt, so a table entry would be wrong.
- together / siliconflow / openrouter: passthrough platforms limited by the
  underlying provider or context window; the generic fallback is safer than
  a guessed number.
- Unverified, do not add until verified: xai, perplexity, stepfun,
  sensenova, kimi-k2.7-code, qwen3-max, qwen3.5 open-weight tiers
  (note: no generic "qwen3" rule exists on purpose — it would misfire on
  the unverified qwen3-max / open-weight models).
"""

from __future__ import annotations

# (provider, model pattern, max_output_tokens)
_RULES: tuple[tuple[str, str, int], ...] = (
    # deepseek (V4 empirically verified: max_tokens=64000 accepted)
    ("deepseek", "deepseek-v4", 64000),
    # zhipu (specific 4.x rules must precede the generic glm- rule)
    ("zhipu", "glm-4.6v", 32768),
    ("zhipu", "glm-4.5", 98304),
    ("zhipu", "glm-4-plus", 4095),
    ("zhipu", "glm-4-air", 4095),
    ("zhipu", "glm-4-flash", 4095),
    ("zhipu", "glm-", 131072),  # glm-5.x / 4.6 / 4.7 = 128K
    # dashscope (aliyun; qwen3-max and qwen3.5 open-weight tiers unverified —
    # deliberately no generic qwen3 rule)
    ("dashscope", "qwen3.7", 65536),
    ("dashscope", "qwen3.6", 65536),
    ("dashscope", "qwen3.5-plus", 65536),
    ("dashscope", "qwen3.5-flash", 65536),
    # kimi (moonshot)
    ("kimi", "kimi-k3", 131072),
    ("kimi", "kimi-k2.6", 65536),
    ("kimi", "kimi-k2.5", 65536),
    # minimax
    ("minimax", "minimax-m3", 131072),
    ("minimax", "minimax-m2", 65536),
    # iflytek
    ("iflytek", "spark-x", 131072),
    # baidu
    ("baidu", "ernie-5", 65536),
    # tencent
    ("tencent", "hunyuan-t1", 65536),
    ("tencent", "hunyuan-a13b", 32768),
    ("tencent", "hunyuan-2.0-thinking", 65536),
    ("tencent", "hunyuan-2.0-instruct", 16384),
    ("tencent", "hy3", 131072),
    # openai
    ("openai", "gpt-5", 128000),
    ("openai", "gpt-4.1", 32768),
    ("openai", "gpt-4o", 16384),
    # OpenAI-compatible gateways (opencode zen/go): scoped gateway-served
    # values (research: tmp/model_research_2026-08-08.json, 2026-08-08).
    # grok-4.5 deliberately unlisted — its 500K max_output equals the context
    # window and looks like a backfilled placeholder, not a verified limit.
    ("openai", "deepseek-v4", 384000),
    ("openai", "glm-5", 131072),
    ("openai", "kimi-k2.7-code", 262144),
    ("openai", "kimi-k2", 65536),
    ("openai", "kimi-k3", 131072),
    ("openai", "qwen3.7", 131072),
    ("openai", "qwen3.6-plus", 65536),
    ("openai", "qwen3.5-plus", 65536),
    ("openai", "minimax-m3", 128000),
    ("openai", "minimax-m2", 131072),
    ("openai", "mimo-v2-omni", 64000),
    ("openai", "mimo-", 131072),
    ("openai", "hy3", 64000),
    # anthropic (haiku is capped at 64K; the generic rule covers the 128K family)
    ("anthropic", "claude-haiku", 65536),
    ("anthropic", "claude-", 128000),
    # cohere (command-a-plus / command-a-reasoning must precede command-a)
    ("cohere", "command-a-plus", 65536),
    ("cohere", "command-a-reasoning", 32768),
    ("cohere", "command-a", 8192),
    ("cohere", "command-r", 4096),
    # google (strong lead — see module docstring; recheck when docs reachable)
    ("google", "gemini-", 65536),
    # groq
    ("groq", "gpt-oss", 65536),
    ("groq", "llama-3.3-70b", 32768),
    ("groq", "llama-3.1-8b", 131072),
    ("groq", "compound", 8192),
    ("groq", "minimax-m2", 131072),
    ("groq", "qwen3.6-27b", 16384),
    # cerebras
    ("cerebras", "gpt-oss", 40960),
    # sambanova (deepseek / llama-3.3-70b are capped unusually low on this platform)
    ("sambanova", "deepseek", 7168),
    ("sambanova", "llama-3.3-70b", 3072),
    ("sambanova", "gpt-oss", 131072),
    ("sambanova", "gemma-4-31b", 131072),
    ("sambanova", "minimax-m2", 196608),
    # deepinfra (platform-wide hard cap; empty pattern is an intentional catch-all)
    ("deepinfra", "", 16384),
    # novita (only glm-5.2 verified)
    ("novita", "glm-5.2", 131072),
    # agnes
    ("agnes", "agnes-", 65536),
    # fireworks (no official per-model limit; safe mid-tier — see docstring)
    ("fireworks", "", 16384),
)


def lookup_max_output(provider: str | None, model_id: str | None) -> int | None:
    """Return the verified max output token limit for a model, or ``None``."""
    if not provider or model_id is None:
        return None
    provider_key = provider.lower()
    model_key = model_id.lower()
    candidates = [(pattern, limit) for rule_provider, pattern, limit in _RULES if rule_provider == provider_key]
    for pattern, limit in candidates:
        if model_key == pattern:
            return limit
    # Single ordered pass for prefix/substring so list order keeps precedence
    # (e.g. "glm-4.6v-11b" hits "glm-4.6v" before the generic "glm-").
    for pattern, limit in candidates:
        if model_key.startswith(pattern) or pattern in model_key:
            return limit
    return None
