"""思考强度策略（V2-002 引入，V2-004 真实化 provider 参数映射）。

两条出参路径：
- 命中 verified reasoning profile（domain/model/known_reasoning.py）→ 按
  profile 的 level_payloads 出参；``budget_ratio`` 占位符在此按 max_output
  钳算成 budget_tokens；clamp 映射（如 max→xhigh）记 warning。
- 未命中 → 现有通用映射（catalog reasoning_parameter + strength 比例）。

旧档位别名（老消息 reasoning_snapshot 复用不炸）：fast→low、standard→medium、
deep→high。不支持的级别抛 ValueError（路由层转 422 + supported_levels），
绝不静默降级为 auto。

Anthropic 要求 ``budget_tokens >= 1024`` 且严格小于 ``max_tokens``：预算钳到
``max_output_tokens - 1``；若 ``max_output_tokens <= 1024``（合法窗口为空），
不静默降级——``provider_parameter`` 返回 None（不发 thinking）并记 warning。

集群弹性解析（resolve_task_reasoning）：席位档位是上限，任务类型决定实际
档位——正文大输出任务（scene_*/rewrite）按席位档位并上调 max_output 预留
思考预算；小型结构 JSON 任务优先 none（reasoning_off 保护）否则 low；
不支持的档位逐级降档记 warning，未知 profile 回退 auto，绝不抛异常。
"""

from __future__ import annotations

import copy
from dataclasses import replace

from proseforge.domain.model.capabilities import ReasoningLevel
from proseforge.domain.model.known_reasoning import ReasoningProfile

_LEGACY_ALIASES = {"fast": "low", "standard": "medium", "deep": "high"}
_STRENGTH = {ReasoningLevel.LOW: 0.25, ReasoningLevel.MEDIUM: 0.5, ReasoningLevel.HIGH: 0.75, ReasoningLevel.MAX: 1.0}
_EFFORT_WORDS = {ReasoningLevel.LOW: "low", ReasoningLevel.MEDIUM: "medium", ReasoningLevel.HIGH: "high", ReasoningLevel.MAX: "high"}
# Anthropic thinking budget_tokens 的 provider 下限。
_ANTHROPIC_MIN_THINKING_BUDGET = 1024


def resolve_reasoning(level: ReasoningLevel | str, capabilities) -> dict[str, object]:
    raw = level.value if isinstance(level, ReasoningLevel) else str(level)
    normalized = _LEGACY_ALIASES.get(raw, raw)
    selected = ReasoningLevel(normalized)
    if selected is ReasoningLevel.AUTO:
        return {"level": selected.value, "parameter": None, "provider_parameter": None, "warnings": []}
    profile = getattr(capabilities, "reasoning_profile", None)
    if profile is not None:
        return _resolve_with_profile(selected, profile, capabilities)
    if not capabilities.supports_reasoning:
        raise ValueError(f"reasoning level {selected.value} is unsupported; use auto")
    if selected is ReasoningLevel.NONE:
        # Generic path: no verified payload shape — sending nothing is the only honest "off".
        return {"level": selected.value, "parameter": None, "provider_parameter": None, "warnings": []}
    if selected not in _STRENGTH:
        raise ValueError(f"reasoning level {selected.value} is unsupported; use auto")
    strength = _STRENGTH[selected]
    parameter = capabilities.reasoning_parameter or "reasoning"
    warnings: list[str] = []
    provider_parameter = _provider_parameter(parameter, strength, selected, capabilities, warnings)
    return {"level": selected.value, "parameter": parameter, "strength": strength, "provider_parameter": provider_parameter, "warnings": warnings}


def _resolve_with_profile(selected: ReasoningLevel, profile: ReasoningProfile, capabilities) -> dict[str, object]:
    warnings: list[str] = []
    level_value = selected.value
    if level_value in profile.level_payloads:
        payload = profile.level_payloads[level_value]
    elif level_value in profile.clamp:
        target = profile.clamp[level_value]
        payload = profile.level_payloads[target]
        warnings.append(f"reasoning level '{level_value}' is clamped to the provider's maximum effort '{target}'")
    else:
        raise ValueError(f"reasoning level {level_value} is unsupported; supported: {','.join(profile.supported_levels)}")
    provider_parameter = _materialize_payload(payload, capabilities, warnings)
    return {"level": level_value, "parameter": profile.parameter, "provider_parameter": provider_parameter, "warnings": warnings}


def reasoning_off_parameter(capabilities) -> dict[str, object] | None:
    """Provider payload that turns thinking OFF, for structured-output agent calls.

    Agent-run tasks (planner/scene/review/rewrite/analyst) emit JSON under a
    fixed max_output_tokens budget. With provider-default thinking ON (e.g.
    deepseek-v4-flash), long reasoning can consume the whole budget and return
    empty content — observed in production as JSONDecodeError at char 0 after
    finish_reason=length. When the verified profile offers a "none" payload
    (deepseek: {"thinking": {"type": "disabled"}}), hand it to the caller;
    otherwise None (nothing honest to send — AUTO behavior unchanged).
    """
    profile = getattr(capabilities, "reasoning_profile", None)
    if profile is None or "none" not in profile.level_payloads:
        return None
    return resolve_reasoning(ReasoningLevel.NONE, capabilities)["provider_parameter"]


# --- Elastic per-task reasoning (cluster runs) -----------------------------
#
# The seat level from the cluster config is a CEILING; the task type decides
# the actual tier so long thinking never burns a small JSON output budget:
# - prose tasks (scene_* drafts, rewrite): the seat level applies, and
#   max_output grows by the tier's reserve ratio so thinking cannot eat the
#   prose budget (split rewriting is a separate work item);
# - every other agent task emits small structured JSON: thinking OFF when the
#   profile offers a none payload (the reasoning_off protection above), else
#   the lowest tier, else auto.
# Step-down order when a profile rejects the requested tier outright (the
# softer cases ride the profile's own clamp mapping, which already warns).
_STEP_DOWN = ("max", "xhigh", "high", "medium", "low")
_PROSE_TASK_PREFIXES = ("scene_",)
_PROSE_TASK_KEYS = frozenset({"rewrite"})
# max_output reserve ratio per resolved tier (thinking scales with effort).
_PROSE_RESERVE_RATIO = {"low": 0.1, "medium": 0.25, "high": 0.5, "xhigh": 0.75, "max": 1.0}


def is_prose_task(task_key: str) -> bool:
    """True for tasks whose payload is long-form prose (chapter drafts)."""
    return task_key in _PROSE_TASK_KEYS or task_key.startswith(_PROSE_TASK_PREFIXES)


def _step_down(requested: str, capabilities, warnings: list[str]) -> dict[str, object] | None:
    """First resolve_reasoning policy that the model accepts, walking down
    from the requested tier; None when nothing below it resolves."""
    tiers = [requested] if requested == "none" else list(_STEP_DOWN[_STEP_DOWN.index(requested):]) if requested in _STEP_DOWN else []
    for tier in tiers:
        try:
            policy = resolve_reasoning(tier, capabilities)
        except ValueError:
            continue
        if tier != requested:
            warnings.append(f"reasoning level '{requested}' stepped down to '{tier}' (nearest supported tier)")
        warnings.extend(str(warning) for warning in policy.get("warnings", []))
        return policy
    return None


def resolve_task_reasoning(task_key: str, level: str, capabilities, base_max_output: int) -> dict[str, object]:
    """Elastic per-task reasoning resolution for agent runs; never raises.

    Returns ``{level, provider_parameter, max_output, warnings}``. Unsupported
    tiers step down to the nearest supported one (warning recorded); unknown
    profiles / non-reasoning models fall back to auto (no payload) — a run
    must never fail because of a reasoning tier.
    """
    warnings: list[str] = []
    policy: dict[str, object] | None = None
    if not is_prose_task(task_key):
        # Structured JSON output: prefer turning thinking OFF entirely when
        # the verified profile offers a none payload (default-on thinking can
        # burn the whole JSON budget — the deepseek-v4 lesson); otherwise the
        # smallest tier.
        off = reasoning_off_parameter(capabilities)
        if off is not None:
            return {"level": "none", "provider_parameter": off, "max_output": base_max_output, "warnings": warnings}
        policy = _step_down("low", capabilities, warnings)
    elif level == ReasoningLevel.AUTO.value:
        # Explicit auto: provider default, no payload, no reserve.
        policy = resolve_reasoning(ReasoningLevel.AUTO, capabilities)
    else:
        policy = _step_down(level, capabilities, warnings)
    if policy is None:
        warnings.append(f"reasoning level '{level}' is unsupported by this model; falling back to auto")
        return {"level": ReasoningLevel.AUTO.value, "provider_parameter": None, "max_output": base_max_output, "warnings": warnings}
    resolved_level = str(policy["level"])
    max_output = base_max_output
    if is_prose_task(task_key) and policy.get("provider_parameter") is not None and resolved_level in _PROSE_RESERVE_RATIO:
        reserve = int(base_max_output * _PROSE_RESERVE_RATIO[resolved_level])
        # Cap at the model's own output ceiling, never below the base budget.
        max_output = max(base_max_output, min(base_max_output + reserve, int(capabilities.max_output_tokens)))
    if int(capabilities.max_output_tokens) != max_output:
        # Re-materialize against the request's real max_output: resolve_reasoning
        # clamps budget-style payloads against the model's ceiling, but
        # Anthropic-style providers reject budget_tokens >= the request's
        # max_tokens (a 16-token classify answer could never carry a budget).
        rematerialized = _step_down(resolved_level, replace(capabilities, max_output_tokens=max_output), [])
        if rematerialized is not None:
            warnings.extend(str(warning) for warning in rematerialized.get("warnings", []) if str(warning) not in warnings)
            policy = rematerialized
    return {"level": resolved_level, "provider_parameter": policy.get("provider_parameter"), "max_output": max_output, "warnings": warnings}


def _clamp_budget(ratio: float, capabilities, warnings: list[str]) -> int | None:
    """Shared budget clamp: >= 1024 and strictly below max_tokens.

    An empty legal window (max_output_tokens <= 1024) yields None — the caller
    must not send a payload the provider would reject.
    """
    if capabilities.max_output_tokens <= _ANTHROPIC_MIN_THINKING_BUDGET:
        # budget 须 ≥ 1024 且 < max_tokens，二者不可兼得——显式关闭并记
        # warning，绝不发出 provider 必拒的载荷。
        warnings.append("thinking disabled: max_output_tokens below provider minimum")
        return None
    budget = max(_ANTHROPIC_MIN_THINKING_BUDGET, int(ratio * capabilities.max_output_tokens))
    return min(budget, capabilities.max_output_tokens - 1)


def _materialize_payload(payload: dict[str, object], capabilities, warnings: list[str]) -> dict[str, object] | None:
    """Resolve budget placeholders into clamped integers; copy everything else.

    Two placeholder shapes: ``budget_ratio`` inside a ``thinking`` object (→
    ``budget_tokens``, Anthropic-style) and a top-level
    ``thinking_budget_ratio`` (→ top-level ``thinking_budget``, dashscope
    qwen3.7/3.6-plus style). Same clamp for both.
    """
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and "budget_ratio" in thinking:
        budget = _clamp_budget(float(thinking["budget_ratio"]), capabilities, warnings)
        if budget is None:
            return None
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}
    if "thinking_budget_ratio" in payload:
        budget = _clamp_budget(float(payload["thinking_budget_ratio"]), capabilities, warnings)
        if budget is None:
            return None
        materialized = {key: copy.deepcopy(value) for key, value in payload.items() if key != "thinking_budget_ratio"}
        materialized["thinking_budget"] = budget
        return materialized
    return copy.deepcopy(payload)


def _provider_parameter(parameter: str, strength: float, selected: ReasoningLevel, capabilities, warnings: list[str]) -> dict[str, object] | None:
    if parameter == "reasoning_effort":
        if selected is ReasoningLevel.MAX:
            warnings.append("reasoning level 'max' is clamped to the provider's maximum effort 'high'")
        return {"reasoning_effort": _EFFORT_WORDS[selected]}
    if parameter == "thinking":
        if capabilities.max_output_tokens <= _ANTHROPIC_MIN_THINKING_BUDGET:
            warnings.append("thinking disabled: max_output_tokens below provider minimum")
            return None
        budget = max(_ANTHROPIC_MIN_THINKING_BUDGET, int(strength * capabilities.max_output_tokens))
        budget = min(budget, capabilities.max_output_tokens - 1)
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}
    if parameter == "thinking_budget":
        return {"thinking_budget": max(1, int(strength * capabilities.max_output_tokens))}
    return {parameter: strength}
