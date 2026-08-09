from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class UsageDelta:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    source: Literal["provider", "estimated", "missing"] = "provider"
    final: bool = False
    provider_request_id: str | None = None
    raw_metadata: dict[str, object] = field(default_factory=dict)

    def as_event_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "source": self.source,
            "final": self.final,
        }
        if self.provider_request_id:
            payload["provider_request_id"] = self.provider_request_id
        return payload


def _integer(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def normalize_usage(data: dict[str, object], *, source: Literal["provider", "estimated", "missing"] = "provider", final: bool = False) -> UsageDelta:
    raw = data.get("usage") if isinstance(data.get("usage"), dict) else data
    values = raw if isinstance(raw, dict) else {}
    input_tokens = _integer(values.get("input_tokens", values.get("prompt_tokens", values.get("inputTokens", 0))))
    output_tokens = _integer(values.get("output_tokens", values.get("completion_tokens", values.get("outputTokens", 0))))
    # Cache-hit counters: anthropic-style flat keys, deepseek/ark
    # prompt_cache_hit_tokens, OpenAI-style nested prompt_tokens_details.
    prompt_details = values.get("prompt_tokens_details")
    nested_cached = prompt_details.get("cached_tokens", 0) if isinstance(prompt_details, dict) else 0
    cached_input_tokens = _integer(values.get("cached_input_tokens", values.get("cache_read_input_tokens", values.get("prompt_cache_hit_tokens", nested_cached))))
    reasoning_tokens = _integer(values.get("reasoning_tokens", values.get("reasoning", 0)))
    total_value = values.get("total_tokens", values.get("totalTokens"))
    total_tokens = _integer(total_value) if total_value is not None else input_tokens + output_tokens
    request_id = values.get("provider_request_id", values.get("request_id", values.get("id")))
    safe_metadata = {key: values[key] for key in ("service_tier", "finish_reason", "cache_creation_input_tokens") if key in values}
    return UsageDelta(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        source=source,
        final=final,
        provider_request_id=str(request_id) if request_id else None,
        raw_metadata=safe_metadata,
    )
