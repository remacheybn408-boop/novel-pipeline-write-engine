from __future__ import annotations

from typing import Any

from proseforge.domain.usage import UsageDelta, normalize_usage

_USAGE_COUNTER_KEYS = ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "inputTokens", "outputTokens", "total_tokens", "totalTokens")


def _raw_has_usage(provider: str, data: dict[str, object]) -> bool:
    """判断 provider 原始响应是否真的携带了 usage 计数。

    有计数 → "provider"；完全没有 → "missing"（区别于显式的 "estimated"），
    这样 provider 不回 usage 时不会被记成虚假的 provider 值。
    """
    usage = data.get("usage")
    if isinstance(usage, dict) and any(value is not None for value in usage.values()):
        return True
    if isinstance(data.get("usageMetadata"), dict):
        return True
    if provider in {"ollama", "vllm"} and ("prompt_eval_count" in data or "eval_count" in data):
        return True
    if provider == "cohere" and isinstance(data.get("billed_units"), dict):
        return True
    # 扁平计数键：部分 provider 事件把 usage 字段提升到了顶层。
    return any(key in data for key in _USAGE_COUNTER_KEYS)


def normalize_provider_usage(provider: str, data: dict[str, object], *, estimated: bool = False, final: bool = False) -> UsageDelta:
    payload: dict[str, Any] = dict(data)
    has_usage = _raw_has_usage(provider, payload)
    metadata = payload.get("usageMetadata")
    if isinstance(metadata, dict):
        payload["usage"] = {
            "input_tokens": metadata.get("promptTokenCount", 0),
            "output_tokens": metadata.get("candidatesTokenCount", 0),
            "cached_input_tokens": metadata.get("cachedContentTokenCount", 0),
            "reasoning_tokens": metadata.get("thoughtsTokenCount", 0),
            "total_tokens": metadata.get("totalTokenCount"),
        }
    elif provider in {"ollama", "vllm"} and "usage" not in payload:
        payload["usage"] = {"input_tokens": payload.get("prompt_eval_count", 0), "output_tokens": payload.get("eval_count", 0)}
    elif provider == "cohere" and "usage" not in payload:
        billed = payload.get("billed_units") if isinstance(payload.get("billed_units"), dict) else payload
        payload["usage"] = {"input_tokens": billed.get("input_tokens", 0), "output_tokens": billed.get("output_tokens", 0)}
    source = "estimated" if estimated else ("provider" if has_usage else "missing")
    return normalize_usage(payload, source=source, final=final)
