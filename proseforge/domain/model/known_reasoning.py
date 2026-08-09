"""Known reasoning-effort profiles for verified providers.

Mirrors known_windows.py: ordered ``(provider, model pattern, profile)`` rules
matched exact first, then prefix/substring in list order. Only tavily-verified
payload shapes are listed — providers without a verified contract stay
auto-only and must not be guessed.

Known gaps (rechecked 2026-07-24, do not add until verified):
- tencent: hy3 is covered below (TokenHub docs); older-platform models expose
  no thinking switch on the OpenAI-compatible endpoint.
- yi: re-verified 2026-07-24 — the official api-reference has no thinking parameter.
- baichuan: officially confirmed unsupported.
- mistral: the magistral family is deprecated and exposes no parameter.
- minimax: m2.x officially cannot disable thinking.
- kimi: k2.7-code is force-thinking (sending disabled errors out); the same
  holds for together-hosted kimi-k2.7-code.
- openrouter: 63 mandatory-reasoning models reject none (platform 400).
- Official "minimal" tiers are generally outside our level enum
  (auto/none/low/medium/high/xhigh/max) and are deliberately not collected.

Payload convention: values in ``level_payloads`` are raw provider payloads
merged into the generation request by the provider adapters. A ``budget_ratio``
key inside a ``thinking`` object is a placeholder resolved by
application/models/reasoning_policy.py into ``budget_tokens`` (Anthropic-style
clamp: >= 1024 and strictly below max_tokens); a top-level
``thinking_budget_ratio`` key resolves the same way into a top-level integer
``thinking_budget``; everything else is static. An empty payload ({}) merges
as a no-op — used where "off" means sending no fields at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReasoningProfile:
    parameter: str
    level_payloads: dict[str, dict[str, object]]
    # requested level -> actual level (e.g. {"max": "xhigh"}); resolution emits a clamp warning
    clamp: dict[str, str] = field(default_factory=dict)

    @property
    def supported_levels(self) -> list[str]:
        extra = [level for level in self.clamp if level not in self.level_payloads]
        return ["auto", *self.level_payloads, *extra]


def _effort(*levels: str) -> dict[str, dict[str, object]]:
    return {level: {"reasoning_effort": level} for level in levels}


def _thinking_levels() -> dict[str, dict[str, object]]:
    return {
        "none": {"thinking": {"type": "disabled"}},
        "low": {"thinking": {"type": "enabled", "budget_ratio": 0.25}},
        "medium": {"thinking": {"type": "enabled", "budget_ratio": 0.5}},
        "high": {"thinking": {"type": "enabled", "budget_ratio": 0.75}},
        "max": {"thinking": {"type": "enabled", "budget_ratio": 1.0}},
    }


def _thinking_toggle() -> dict[str, dict[str, object]]:
    return {
        "none": {"thinking": {"type": "disabled"}},
        "high": {"thinking": {"type": "enabled"}},
    }


def _enable_thinking_toggle() -> dict[str, dict[str, object]]:
    return {
        "none": {"enable_thinking": False},
        "high": {"enable_thinking": True},
    }


def _chat_template_toggle() -> dict[str, dict[str, object]]:
    return {
        "none": {"chat_template_kwargs": {"enable_thinking": False}},
        "high": {"chat_template_kwargs": {"enable_thinking": True}},
    }


def _thinking_budget_levels() -> dict[str, dict[str, object]]:
    # enable_thinking bool + top-level thinking_budget placeholder (dashscope qwen3.7/3.6-plus)
    return {
        "none": {"enable_thinking": False},
        "low": {"enable_thinking": True, "thinking_budget_ratio": 0.25},
        "medium": {"enable_thinking": True, "thinking_budget_ratio": 0.5},
        "high": {"enable_thinking": True, "thinking_budget_ratio": 0.75},
        "max": {"enable_thinking": True, "thinking_budget_ratio": 1.0},
    }


def _agnes_flash_levels() -> dict[str, dict[str, object]]:
    # Agnes flash: continuous thinking budget_tokens; none uses the chat_template toggle
    return {
        "none": {"chat_template_kwargs": {"enable_thinking": False}},
        "low": {"thinking": {"type": "enabled", "budget_ratio": 0.25}},
        "medium": {"thinking": {"type": "enabled", "budget_ratio": 0.5}},
        "high": {"thinking": {"type": "enabled", "budget_ratio": 0.75}},
        "max": {"thinking": {"type": "enabled", "budget_ratio": 1.0}},
    }


def _adaptive_effort_levels(*levels: str) -> dict[str, dict[str, object]]:
    # Anthropic 4.6+ style: adaptive thinking + effort under output_config
    if not levels:
        levels = ("low", "medium", "high", "xhigh", "max")
    return {level: {"thinking": {"type": "adaptive"}, "output_config": {"effort": level}} for level in levels}


def _reasoning_enabled_toggle() -> dict[str, dict[str, object]]:
    # Together-style boolean switch nested under reasoning.enabled
    return {"none": {"reasoning": {"enabled": False}}, "high": {"reasoning": {"enabled": True}}}


def _ollama_think_levels() -> dict[str, dict[str, object]]:
    # Ollama think field: bool off switch or a level string
    return {"none": {"think": False}, **{level: {"think": level} for level in ("low", "medium", "high", "max")}}


def _openrouter_effort(*levels: str) -> dict[str, dict[str, object]]:
    # OpenRouter's unified reasoning abstraction nests the word under reasoning.effort
    return {level: {"reasoning": {"effort": level}} for level in levels}


# (provider, model pattern, profile) — exact match first, then prefix/substring in order
_RULES: tuple[tuple[str, str, ReasoningProfile], ...] = (
    # OpenAI (official + Azure docs, 2026-07): gpt-5.6 carries the full
    # six-tier reasoning_effort; gpt-5.1 tops out at high (xhigh is codex-max
    # only); the original gpt-5 supports only low/medium/high (no none, no
    # xhigh). o1 is deliberately NOT listed: o1-mini/preview reject the
    # parameter with an error.
    ("openai", "gpt-5.6", ReasoningProfile("reasoning_effort", _effort("none", "low", "medium", "high", "xhigh", "max"))),
    ("openai", "gpt-5.1", ReasoningProfile("reasoning_effort", _effort("none", "low", "medium", "high"))),
    ("openai", "gpt-5", ReasoningProfile("reasoning_effort", _effort("low", "medium", "high"))),
    ("openai", "o3", ReasoningProfile("reasoning_effort", _effort("low", "medium", "high"))),
    ("openai", "o4", ReasoningProfile("reasoning_effort", _effort("low", "medium", "high"))),
    # OpenAI-compatible gateways serving the deepseek-v4 family (opencode
    # zen/go verified live 2026-08-08: reasoning.effort=none → 200). Default-on
    # thinking burns the whole structured-output budget → empty content
    # (JSONDecodeError char 0) on analyst/review JSON tasks.
    # Tier data: tmp/model_research_2026-08-08.json (2026-08-08 swarm research).
    # flash: none/low/high/max (medium→high, xhigh→high); pro: none/high/max
    # (low/medium→high, xhigh→max); the generic family rule mirrors pro.
    ("openai", "deepseek-v4-flash", ReasoningProfile("reasoning_effort", _effort("none", "low", "high", "max"), clamp={"medium": "high", "xhigh": "high"})),
    ("openai", "deepseek-v4-pro", ReasoningProfile("reasoning_effort", _effort("none", "high", "max"), clamp={"low": "high", "medium": "high", "xhigh": "max"})),
    ("openai", "deepseek-v4", ReasoningProfile("reasoning_effort", _effort("none", "high", "max"), clamp={"low": "high", "medium": "high", "xhigh": "max"})),
    # Gateway-served zhipu GLM: 5.2 mirrors the official zhipu entry
    # (effort needs thinking enabled); glm-5/5.1 are thinking toggles.
    ("openai", "glm-5.2", ReasoningProfile("reasoning_effort", {
        "none": {"thinking": {"type": "disabled"}},
        "high": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        "max": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
    })),
    ("openai", "glm-5", ReasoningProfile("thinking", _thinking_toggle())),
    # Gateway-served Kimi: k2.5/k2.6 thinking toggles; k3 top-level
    # reasoning_effort low/high/max. kimi-k2.7-code is force-thinking
    # (sending disabled errors out) — deliberately not listed, stays auto-only.
    ("openai", "kimi-k2.6", ReasoningProfile("thinking", _thinking_toggle())),
    ("openai", "kimi-k2.5", ReasoningProfile("thinking", _thinking_toggle())),
    ("openai", "kimi-k3", ReasoningProfile("reasoning_effort", _effort("low", "high", "max"))),
    # Gateway-served qwen3.5-plus/3.6-plus: enable_thinking + top-level
    # thinking_budget (same shape as the dashscope entries). qwen3.7 routes
    # over the anthropic channel on zen and its thinking shape is unverified —
    # deliberately not listed until live-tested.
    ("openai", "qwen3.5-plus", ReasoningProfile("thinking_budget", _thinking_budget_levels())),
    ("openai", "qwen3.6-plus", ReasoningProfile("thinking_budget", _thinking_budget_levels())),
    # Gateway-served MiniMax M3: none=disabled, high=adaptive (mirrors the
    # official minimax entry). m2.x cannot disable thinking — stays auto-only.
    ("openai", "minimax-m3", ReasoningProfile("thinking", {"none": {"thinking": {"type": "disabled"}}, "high": {"thinking": {"type": "adaptive"}}})),
    # Gateway-served Xiaomi MiMo: v2.5-pro has effort tiers (medium confidence,
    # gateway mapping); the rest are none/high toggles. mimo-v2.5-pro must
    # precede the wider mimo-v2 rule.
    ("openai", "mimo-v2.5-pro", ReasoningProfile("reasoning_effort", _effort("low", "medium", "high"))),
    ("openai", "mimo-v2", ReasoningProfile("reasoning_effort", _effort("none", "high"))),
    # Gateway-served Tencent hy3: no_think/low/high (gateway value wins over
    # the lab's extra medium tier); medium clamps to high. Every effort tier
    # must also enable thinking (default disabled), mirroring the tencent rule.
    ("openai", "hy3", ReasoningProfile("reasoning_effort", {
        "none": {"thinking": {"type": "disabled"}},
        "low": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        "high": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
    }, clamp={"medium": "high"})),
    # Gateway-served xAI grok-4.5: reasoning cannot be disabled (no none).
    ("openai", "grok-4.5", ReasoningProfile("reasoning_effort", _effort("low", "medium", "high"))),
    # Anthropic (2026-07): fable-5 / mythos run adaptive thinking always-on —
    # it cannot be disabled, so no none tier; effort rides in output_config.
    ("anthropic", "claude-fable-5", ReasoningProfile("thinking", _adaptive_effort_levels())),
    ("anthropic", "claude-mythos", ReasoningProfile("thinking", _adaptive_effort_levels())),
    # opus-4-8 / 4-7: thinking defaults off — none sends an empty payload
    # (no fields at all), the levels send adaptive + effort.
    ("anthropic", "claude-opus-4-8", ReasoningProfile("thinking", {"none": {}, **_adaptive_effort_levels()})),
    ("anthropic", "claude-opus-4-7", ReasoningProfile("thinking", {"none": {}, **_adaptive_effort_levels()})),
    # sonnet-5: explicit disabled type for none.
    ("anthropic", "claude-sonnet-5", ReasoningProfile("thinking", {"none": {"thinking": {"type": "disabled"}}, **_adaptive_effort_levels()})),
    # opus-4-6 / sonnet-4-6: same shape but no xhigh tier.
    ("anthropic", "claude-opus-4-6", ReasoningProfile("thinking", {"none": {"thinking": {"type": "disabled"}}, **_adaptive_effort_levels("low", "medium", "high", "max")})),
    ("anthropic", "claude-sonnet-4-6", ReasoningProfile("thinking", {"none": {"thinking": {"type": "disabled"}}, **_adaptive_effort_levels("low", "medium", "high", "max")})),
    # Generic rule covers ONLY claude-4.5 and earlier: 4.6+ rejects
    # budget_tokens with a 400, so newer models must be claimed by the
    # dedicated rules above — keep this one last for the provider.
    ("anthropic", "claude-", ReasoningProfile("thinking", _thinking_levels())),
    # Google Gemini 3.x: thinking_level. 3.5/3.6-flash and 3.1-pro rules must
    # precede the gemini-3 generic. gemini-2.5 uses the legacy thinkingBudget
    # API and is not covered; the official minimal tier is outside our enum.
    ("google", "gemini-3.6-flash", ReasoningProfile("thinking_level", {level: {"thinking_level": level} for level in ("low", "medium", "high")})),
    ("google", "gemini-3.5-flash", ReasoningProfile("thinking_level", {level: {"thinking_level": level} for level in ("low", "medium", "high")})),
    ("google", "gemini-3.1-pro", ReasoningProfile("thinking_level", {level: {"thinking_level": level} for level in ("low", "medium", "high")})),
    # Google Gemini 3: thinking_level; Flash also has medium, Pro only low/high (cannot disable)
    ("google", "gemini-3-flash", ReasoningProfile("thinking_level", {level: {"thinking_level": level} for level in ("low", "medium", "high")})),
    ("google", "gemini-3", ReasoningProfile("thinking_level", {level: {"thinking_level": level} for level in ("low", "high")})),
    # xAI (2026-07): grok-4.3 can disable reasoning; grok-4.5 defaults to high
    # and cannot be disabled. The original grok-4 was retired 2026-05.
    ("xai", "grok-4.3", ReasoningProfile("reasoning_effort", _effort("none", "low", "medium", "high"))),
    ("xai", "grok-4.5", ReasoningProfile("reasoning_effort", _effort("low", "medium", "high"))),
    # Volcengine doubao-seed-2: none=disabled + reasoning_effort low/medium/high
    ("volcengine", "doubao-seed-2", ReasoningProfile("reasoning_effort", {"none": {"thinking": {"type": "disabled"}}, **_effort("low", "medium", "high")})),
    # Volcengine doubao-seed-1.x: thinking.type boolean toggle
    ("volcengine", "doubao-seed-1", ReasoningProfile("thinking", _thinking_toggle())),
    # Volcengine-hosted GLM-5.2: full six tiers (officially none/xhigh/max are
    # only offered on glm-5-2-260617 and the deepseek-v4 family).
    ("volcengine", "glm-5-2", ReasoningProfile("reasoning_effort", _effort("none", "low", "medium", "high", "xhigh", "max"))),
    # Tencent hy3 (TokenHub docs 2026-07): thinking defaults to disabled, so
    # every effort tier must also enable it. Older-platform a13b exposes
    # EnableThinking only on the TC3-signed API — not on the OpenAI-compatible
    # endpoint, so no rule; t1/turbos have no documented switch (auto-only).
    ("tencent", "hy3", ReasoningProfile("reasoning_effort", {
        "none": {"thinking": {"type": "disabled"}},
        "low": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        "medium": {"thinking": {"type": "enabled"}, "reasoning_effort": "medium"},
        "high": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
    })),
    # Aliyun dashscope qwen3.7 / qwen3.6-plus (2026-07): enable_thinking bool +
    # top-level thinking_budget int (continuous; the ratio placeholder is
    # resolved by reasoning_policy with the same clamp as budget_tokens).
    ("dashscope", "qwen3.7", ReasoningProfile("thinking_budget", _thinking_budget_levels())),
    ("dashscope", "qwen3.6-plus", ReasoningProfile("thinking_budget", _thinking_budget_levels())),
    # Aliyun qwen3.8-max-preview: reasoning_effort low/medium/xhigh only
    # (xhigh is the default and mutually exclusive with thinking_budget);
    # high and max clamp to xhigh.
    ("dashscope", "qwen3.8", ReasoningProfile("reasoning_effort", _effort("low", "medium", "xhigh"), clamp={"max": "xhigh", "high": "xhigh"})),
    # Aliyun dashscope qwen3: enable_thinking boolean
    ("dashscope", "qwen3", ReasoningProfile("enable_thinking", {"none": {"enable_thinking": False}, "high": {"enable_thinking": True}})),
    # Zhipu GLM-5.2+ (docs.bigmodel.cn 2026-07): official enum
    # max/xhigh/high/medium/low/minimal/none maps to effective tiers
    # max/high/none (xhigh→max, low/medium→high, minimal→none); effort needs
    # thinking enabled (default on for 5.2). Must precede the generic glm- rule.
    ("zhipu", "glm-5.2", ReasoningProfile("reasoning_effort", {
        "none": {"thinking": {"type": "disabled"}},
        "high": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        "max": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
    })),
    # Zhipu GLM: thinking.type boolean toggle
    ("zhipu", "glm-", ReasoningProfile("thinking", _thinking_toggle())),
    # DeepSeek v4: none=thinking disabled; high/max map to reasoning_effort high/max
    # (server folds low/medium into high and xhigh into max, so only these three levels are advertised)
    ("deepseek", "deepseek-v4", ReasoningProfile("reasoning_effort", {"none": {"thinking": {"type": "disabled"}}, "high": {"reasoning_effort": "high"}, "max": {"reasoning_effort": "max"}})),
    # Kimi k2.6/k2.5: thinking.type boolean toggle (rules must precede any wider kimi pattern).
    # Constraint: k2.x rejects requests carrying both thinking and reasoning_effort —
    # the table sends only thinking for k2.5/k2.6 and only reasoning_effort for k3, so it holds by construction.
    ("kimi", "kimi-k2.6", ReasoningProfile("thinking", _thinking_toggle())),
    ("kimi", "kimi-k2.5", ReasoningProfile("thinking", _thinking_toggle())),
    # Kimi k3: top-level reasoning_effort low/high/max (k2.7-code is force-thinking, auto-only, intentionally not listed)
    ("kimi", "kimi-k3", ReasoningProfile("reasoning_effort", _effort("low", "high", "max"))),
    # Baidu ERNIE 5.1: no official off switch — thinking_budget only (official
    # minimum 100; our clamp floor of 1024 stays safely above it). Must
    # precede the ernie-5 rule.
    ("baidu", "ernie-5.1", ReasoningProfile("thinking_budget", {
        "low": {"thinking_budget_ratio": 0.25},
        "medium": {"thinking_budget_ratio": 0.5},
        "high": {"thinking_budget_ratio": 0.75},
        "max": {"thinking_budget_ratio": 1.0},
    })),
    # Baidu ERNIE 5: enable_thinking boolean (qianfan docs; ernie-5.0-thinking-preview defaults on)
    ("baidu", "ernie-5", ReasoningProfile("enable_thinking", _enable_thinking_toggle())),
    # Baidu-hosted models (qianfan, 2026-07)
    ("baidu", "deepseek-v4", ReasoningProfile("reasoning_effort", {"none": {"thinking": {"type": "disabled"}}, "high": {"reasoning_effort": "high"}, "max": {"reasoning_effort": "max"}})),
    ("baidu", "deepseek-v3.2", ReasoningProfile("thinking", _thinking_toggle())),
    ("baidu", "glm-5", ReasoningProfile("thinking", _thinking_toggle())),
    ("baidu", "kimi-k2.5", ReasoningProfile("thinking", _thinking_toggle())),
    ("baidu", "qwen3", ReasoningProfile("enable_thinking", _enable_thinking_toggle())),
    # MiniMax M3: none=disabled, high=adaptive. M2.x cannot disable thinking —
    # deliberately no "minimax-" catch-all, M2 stays auto-only.
    ("minimax", "minimax-m3", ReasoningProfile("thinking", {"none": {"thinking": {"type": "disabled"}}, "high": {"thinking": {"type": "adaptive"}}})),
    # StepFun: step-3.7 three levels; step-3.5-flash-2603 officially only low/high
    ("stepfun", "step-3.7", ReasoningProfile("reasoning_effort", _effort("low", "medium", "high"))),
    ("stepfun", "step-3.5", ReasoningProfile("reasoning_effort", _effort("low", "high"))),
    # iFlytek Spark-X: thinking.type boolean toggle
    ("iflytek", "spark-x", ReasoningProfile("thinking", _thinking_toggle())),
    # SenseNova 6.7: reasoning_effort four levels incl. none (default medium)
    ("sensenova", "sensenova-6.7", ReasoningProfile("reasoning_effort", _effort("none", "low", "medium", "high"))),
    # SenseNova-hosted DeepSeek v4
    ("sensenova", "deepseek-v4", ReasoningProfile("reasoning_effort", _effort("none", "low", "medium", "high"))),
    # Mistral: official enum is high|none only — low/medium must raise
    ("mistral", "mistral-small-latest", ReasoningProfile("reasoning_effort", _effort("none", "high"))),
    ("mistral", "mistral-medium-3-5", ReasoningProfile("reasoning_effort", _effort("none", "high"))),
    # Cohere command-a-plus: none/high only; must precede command-a-reasoning
    ("cohere", "command-a-plus", ReasoningProfile("reasoning_effort", _effort("none", "high"))),
    # Cohere command-a-reasoning: compatible endpoint documents none/high only
    ("cohere", "command-a-reasoning", ReasoningProfile("reasoning_effort", _effort("none", "high"))),
    # Perplexity sonar: low/medium/high only; official bottom level is "minimal"
    # (reduced reasoning, not off) so none is deliberately not advertised.
    ("perplexity", "sonar", ReasoningProfile("reasoning_effort", _effort("low", "medium", "high"))),
    # OpenRouter: unified reasoning abstraction across all routed models.
    # The empty pattern is an intentional provider-level catch-all:
    # startswith("") and "" in s are both True, so any model id matches.
    ("openrouter", "", ReasoningProfile("reasoning", _openrouter_effort("none", "low", "medium", "high", "xhigh", "max"))),
    # Groq gpt-oss: cannot disable reasoning, no none
    ("groq", "gpt-oss", ReasoningProfile("reasoning_effort", _effort("low", "medium", "high"))),
    # Groq qwen3.6-27b: only none/default two tiers
    ("groq", "qwen3.6-27b", ReasoningProfile("reasoning_effort", {"none": {"reasoning_effort": "none"}, "high": {"reasoning_effort": "default"}})),
    # Together: gpt-oss effort levels; deepseek-v4 none=reasoning disabled + effort high/max
    ("together", "gpt-oss", ReasoningProfile("reasoning_effort", _effort("low", "medium", "high"))),
    ("together", "deepseek-v4", ReasoningProfile("reasoning_effort", {"none": {"reasoning": {"enabled": False}}, "high": {"reasoning_effort": "high"}, "max": {"reasoning_effort": "max"}})),
    # Together boolean-switch models (reasoning.enabled); glm-5.2 must precede glm-5
    ("together", "qwen3.5", ReasoningProfile("reasoning", _reasoning_enabled_toggle())),
    ("together", "qwen3.6-plus", ReasoningProfile("reasoning", _reasoning_enabled_toggle())),
    ("together", "kimi-k2.6", ReasoningProfile("reasoning", _reasoning_enabled_toggle())),
    ("together", "glm-5.2", ReasoningProfile("reasoning_effort", _effort("high", "max"))),
    ("together", "glm-5", ReasoningProfile("reasoning", _reasoning_enabled_toggle())),
    # Fireworks: glm-5.2 must precede the wider glm- rule
    ("fireworks", "deepseek-v4", ReasoningProfile("reasoning_effort", _effort("none", "high", "max"))),
    ("fireworks", "glm-5.2", ReasoningProfile("reasoning_effort", _effort("none", "high", "max"))),
    ("fireworks", "glm-", ReasoningProfile("reasoning_effort", _effort("none", "high"))),
    ("fireworks", "gpt-oss", ReasoningProfile("reasoning_effort", _effort("low", "medium", "high"))),
    ("fireworks", "minimax-m2", ReasoningProfile("reasoning_effort", _effort("low", "medium", "high"))),
    ("fireworks", "qwen3", ReasoningProfile("reasoning_effort", _effort("none", "low", "medium", "high"))),
    # Cerebras: zai-glm-4.7 supports only switching thinking off, no effort levels
    ("cerebras", "gpt-oss", ReasoningProfile("reasoning_effort", _effort("low", "medium", "high"))),
    ("cerebras", "gemma-4-31b", ReasoningProfile("reasoning_effort", _effort("none", "low", "medium", "high"))),
    ("cerebras", "zai-glm-4.7", ReasoningProfile("reasoning_effort", {"none": {"reasoning_effort": "none"}})),
    # SambaNova: gpt-oss levels per the official SDK OpenAPI parameter definition
    ("sambanova", "gpt-oss", ReasoningProfile("reasoning_effort", _effort("low", "medium", "high"))),
    ("sambanova", "deepseek", ReasoningProfile("chat_template_kwargs", _chat_template_toggle())),
    # SambaNova gemma-4: chat_template_kwargs enable_thinking boolean
    ("sambanova", "gemma-4", ReasoningProfile("chat_template_kwargs", _chat_template_toggle())),
    # DeepInfra deepseek-r1: official API offers seven tiers — minimal is
    # outside our enum and deliberately not collected.
    ("deepinfra", "deepseek-r1", ReasoningProfile("reasoning_effort", _effort("none", "low", "medium", "high", "xhigh", "max"))),
    # DeepInfra step-3.7: always on, cannot be disabled
    ("deepinfra", "step-3.7", ReasoningProfile("reasoning_effort", _effort("low", "medium", "high"))),
    # Novita: enable_thinking boolean for GLM-4.5 / DeepSeek-V3.1 / V3.2
    # (the official list has no plain v3)
    ("novita", "glm-4.5", ReasoningProfile("enable_thinking", _enable_thinking_toggle())),
    ("novita", "deepseek-v3.1", ReasoningProfile("enable_thinking", _enable_thinking_toggle())),
    ("novita", "deepseek-v3.2", ReasoningProfile("enable_thinking", _enable_thinking_toggle())),
    # SiliconFlow: only deepseek-v4 Flash is officially documented (high/max;
    # low/medium are folded into high); Pro is unverified. qwen3.5 must
    # precede the qwen3 generic.
    ("siliconflow", "deepseek-v4-flash", ReasoningProfile("reasoning_effort", _effort("high", "max"))),
    ("siliconflow", "qwen3.5", ReasoningProfile("enable_thinking", _enable_thinking_toggle())),
    ("siliconflow", "qwen3", ReasoningProfile("enable_thinking", _enable_thinking_toggle())),
    ("siliconflow", "glm-", ReasoningProfile("enable_thinking", _enable_thinking_toggle())),
    ("siliconflow", "deepseek-v3", ReasoningProfile("enable_thinking", _enable_thinking_toggle())),
    # Agnes flash family 2.0/2.5 (official wiki 2026-07): continuous thinking
    # budget_tokens (no documented range — "start at 2048, raise as needed",
    # hence the budget_ratio placeholder); chat_template toggle also accepted
    # and used for none. pro-alpha has no official example — stays on the
    # generic toggle below. Flash rules must precede it.
    ("agnes", "agnes-2.5-flash", ReasoningProfile("thinking", _agnes_flash_levels())),
    ("agnes", "agnes-2.0-flash", ReasoningProfile("thinking", _agnes_flash_levels())),
    # Agnes: chat_template_kwargs enable_thinking boolean
    ("agnes", "agnes-", ReasoningProfile("chat_template_kwargs", _chat_template_toggle())),
    # Ollama: official think field (bool or "low/medium/high/max" string).
    # gpt-oss cannot disable thinking.
    ("ollama", "gpt-oss", ReasoningProfile("think", {level: {"think": level} for level in ("low", "medium", "high")})),
    ("ollama", "qwen3", ReasoningProfile("think", _ollama_think_levels())),
    ("ollama", "deepseek-r1", ReasoningProfile("think", _ollama_think_levels())),
    ("ollama", "deepseek-v3.1", ReasoningProfile("think", _ollama_think_levels())),
    # vLLM: server-side reasoning parser maps reasoning_effort
    # low/medium/high to enable_thinking=true and none to false (generic
    # injection). The empty pattern is a deliberate catch-all — only
    # meaningful for models served with a reasoning parser enabled.
    ("vllm", "", ReasoningProfile("reasoning_effort", _effort("none", "low", "medium", "high"))),
)


def lookup_reasoning(provider: str | None, model_id: str | None) -> ReasoningProfile | None:
    """Return the verified reasoning profile for a model, or ``None`` (auto-only)."""
    if not provider or not model_id:
        return None
    provider_key = provider.lower()
    model_key = model_id.lower()
    candidates = [(pattern, profile) for rule_provider, pattern, profile in _RULES if rule_provider == provider_key]
    for pattern, profile in candidates:
        if model_key == pattern:
            return profile
    for pattern, profile in candidates:
        if model_key.startswith(pattern) or pattern in model_key:
            return profile
    return None
