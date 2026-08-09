"""Provider 特定的思考强度映射（V2-004）。

catalog `reasoning_parameter` 决定载荷形状：OpenAI reasoning_effort /
Anthropic thinking / Google thinking_budget；其它参数名按 strength 透传。
不支持的级别抛 ValueError（路由层转 422 + supported_levels），绝不静默降级。

后半部分钉线上载荷：provider 实现必须把 ``GenerationRequest.reasoning``
按 catalog 参数名序列化进请求体；None（AUTO/不支持时的取值）一律不发，
由 provider 默认值接管——绝不允许选了 deep 和 fast 发出同样的请求。
"""

import json

import httpx
import pytest
import respx

from proseforge.application.models.reasoning_policy import resolve_reasoning
from proseforge.domain.model.capabilities import ModelCapabilities
from proseforge.domain.ports.model_provider import GenerationRequest
from proseforge.providers.anthropic import AnthropicProvider
from proseforge.providers.google import GoogleProvider
from proseforge.providers.openai import OpenAIProvider


def _caps(parameter: str | None, *, max_output: int = 4096) -> ModelCapabilities:
    return ModelCapabilities(128000, max_output, True, parameter, False, False, "catalog")


def test_openai_reasoning_effort_maps_levels_to_effort_words():
    caps = _caps("reasoning_effort")

    assert resolve_reasoning("fast", caps)["provider_parameter"] == {"reasoning_effort": "low"}
    assert resolve_reasoning("standard", caps)["provider_parameter"] == {"reasoning_effort": "medium"}
    assert resolve_reasoning("deep", caps)["provider_parameter"] == {"reasoning_effort": "high"}


def test_openai_max_clamps_to_high_and_records_warning():
    policy = resolve_reasoning("max", _caps("reasoning_effort"))

    assert policy["provider_parameter"] == {"reasoning_effort": "high"}
    assert policy["warnings"], "clamping must be recorded, never silent"


def test_anthropic_thinking_maps_to_budget_tokens():
    policy = resolve_reasoning("deep", _caps("thinking", max_output=4096))

    assert policy["provider_parameter"] == {"thinking": {"type": "enabled", "budget_tokens": int(0.75 * 4096)}}


def test_anthropic_max_budget_stays_strictly_below_max_tokens():
    # Anthropic 要求 budget_tokens < max_tokens：MAX（strength 1.0）必须钳到
    # max_output_tokens - 1，否则线上 400。
    policy = resolve_reasoning("max", _caps("thinking", max_output=4096))

    assert policy["provider_parameter"] == {"thinking": {"type": "enabled", "budget_tokens": 4095}}


def test_anthropic_standard_budget_hits_provider_minimum_and_stays_valid():
    # 0.5 × 2048 = 1024 = provider 下限；钳制上界 2047 不影响，载荷合法。
    policy = resolve_reasoning("standard", _caps("thinking", max_output=2048))

    assert policy["provider_parameter"] == {"thinking": {"type": "enabled", "budget_tokens": 1024}}


def test_anthropic_thinking_disabled_when_max_output_below_provider_minimum():
    # max_output_tokens ≤ 1024 时合法窗口为空（budget ≥ 1024 且 < max_tokens
    # 不可兼得）——不静默降级，显式不发 thinking 并记 warning。
    for level in ("fast", "standard", "deep", "max"):
        policy = resolve_reasoning(level, _caps("thinking", max_output=1024))

        assert policy["provider_parameter"] is None
        assert any("below provider minimum" in warning for warning in policy["warnings"])


def test_google_thinking_budget_maps_to_integer_tokens():
    policy = resolve_reasoning("standard", _caps("thinking_budget", max_output=4096))

    assert policy["provider_parameter"] == {"thinking_budget": int(0.5 * 4096)}


def test_unknown_parameter_passes_strength_through():
    policy = resolve_reasoning("deep", _caps("effort"))

    assert policy["provider_parameter"] == {"effort": 0.75}
    assert policy["parameter"] == "effort"


def test_auto_never_claims_a_provider_parameter():
    policy = resolve_reasoning("auto", _caps("reasoning_effort"))

    assert policy["parameter"] is None
    assert policy["provider_parameter"] is None


def test_unsupported_level_raises_instead_of_silent_downgrade():
    caps = ModelCapabilities(8192, 1024, False, None, False, False, "fallback")

    with pytest.raises(ValueError, match="unsupported"):
        resolve_reasoning("max", caps)
    with pytest.raises(ValueError, match="unsupported"):
        resolve_reasoning("fast", caps)


# --- 线上载荷：request.reasoning 必须落到序列化后的请求体 -----------------

_OPENAI_SSE = (
    'data: {"type":"response.created"}\n'
    'data: {"type":"response.output_text.delta","delta":"Hi"}\n'
    'data: {"type":"response.completed"}\n'
    "data: [DONE]\n"
)
_ANTHROPIC_SSE = 'data: {"type":"content_block_delta","delta":{"text":"Hi"}}\n\ndata: {"type":"message_stop"}\n\n'
_GOOGLE_SSE = 'data: {"candidates":[{"content":{"parts":[{"text":"Hi"}]}}]}\n\n'


def _request(reasoning: dict[str, object] | None) -> GenerationRequest:
    return GenerationRequest(
        "test-model",
        ({"text": "Be concise"},),
        ({"role": "user", "text": "Hi"},),
        max_output_tokens=4096,
        reasoning=reasoning,
    )


@pytest.mark.asyncio
@respx.mock
async def test_openai_wire_payload_carries_reasoning_effort():
    route = respx.post("https://api.test/v1/responses").mock(
        return_value=httpx.Response(200, text=_OPENAI_SSE, headers={"content-type": "text/event-stream"})
    )
    provider = OpenAIProvider("secret", "https://api.test/v1")

    [event async for event in provider.stream(_request({"reasoning_effort": "high"}))]

    body = json.loads(route.calls[0].request.content)
    # Responses API 只认嵌套形状 reasoning: {effort}；顶层 reasoning_effort 会被拒。
    assert body["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in body


@pytest.mark.asyncio
@respx.mock
async def test_openai_wire_payload_passes_unknown_reasoning_keys_through_flat():
    route = respx.post("https://api.test/v1/responses").mock(
        return_value=httpx.Response(200, text=_OPENAI_SSE, headers={"content-type": "text/event-stream"})
    )
    provider = OpenAIProvider("secret", "https://api.test/v1")

    [event async for event in provider.stream(_request({"effort": 0.75}))]

    body = json.loads(route.calls[0].request.content)
    assert body["effort"] == 0.75
    assert "reasoning" not in body


@pytest.mark.asyncio
@respx.mock
async def test_openai_wire_payload_omits_reasoning_when_none():
    route = respx.post("https://api.test/v1/responses").mock(
        return_value=httpx.Response(200, text=_OPENAI_SSE, headers={"content-type": "text/event-stream"})
    )
    provider = OpenAIProvider("secret", "https://api.test/v1")

    [event async for event in provider.stream(_request(None))]  # AUTO → provider_parameter=None

    body = json.loads(route.calls[0].request.content)
    assert "reasoning_effort" not in body
    assert "reasoning" not in body


@pytest.mark.asyncio
@respx.mock
async def test_anthropic_wire_payload_carries_thinking():
    route = respx.post("https://anthropic.test/v1/messages").mock(
        return_value=httpx.Response(200, content=_ANTHROPIC_SSE, headers={"content-type": "text/event-stream"})
    )
    provider = AnthropicProvider("secret", base_url="https://anthropic.test/v1")

    [event async for event in provider.stream(_request({"thinking": {"type": "enabled", "budget_tokens": 2048}}))]

    body = json.loads(route.calls[0].request.content)
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 2048}


@pytest.mark.asyncio
@respx.mock
async def test_anthropic_wire_payload_omits_thinking_when_none():
    route = respx.post("https://anthropic.test/v1/messages").mock(
        return_value=httpx.Response(200, content=_ANTHROPIC_SSE, headers={"content-type": "text/event-stream"})
    )
    provider = AnthropicProvider("secret", base_url="https://anthropic.test/v1")

    [event async for event in provider.stream(_request(None))]

    body = json.loads(route.calls[0].request.content)
    assert "thinking" not in body


@pytest.mark.asyncio
@respx.mock
async def test_google_wire_payload_carries_thinking_budget():
    route = respx.post("https://google.test/v1beta/models/test-model:streamGenerateContent?alt=sse").mock(
        return_value=httpx.Response(200, content=_GOOGLE_SSE, headers={"content-type": "text/event-stream"})
    )
    provider = GoogleProvider("secret", base_url="https://google.test/v1beta")

    [event async for event in provider.stream(_request({"thinking_budget": 2048}))]

    body = json.loads(route.calls[0].request.content)
    # Gemini API 只认 generationConfig.thinkingConfig.thinkingBudget；顶层键无效。
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 2048}
    assert "thinking_budget" not in body


@pytest.mark.asyncio
@respx.mock
async def test_google_wire_payload_omits_thinking_budget_when_none():
    route = respx.post("https://google.test/v1beta/models/test-model:streamGenerateContent?alt=sse").mock(
        return_value=httpx.Response(200, content=_GOOGLE_SSE, headers={"content-type": "text/event-stream"})
    )
    provider = GoogleProvider("secret", base_url="https://google.test/v1beta")

    [event async for event in provider.stream(_request(None))]

    body = json.loads(route.calls[0].request.content)
    assert "thinking_budget" not in body
    assert "generationConfig" not in body


@pytest.mark.asyncio
@respx.mock
async def test_google_wire_payload_passes_unknown_reasoning_keys_through_flat():
    route = respx.post("https://google.test/v1beta/models/test-model:streamGenerateContent?alt=sse").mock(
        return_value=httpx.Response(200, content=_GOOGLE_SSE, headers={"content-type": "text/event-stream"})
    )
    provider = GoogleProvider("secret", base_url="https://google.test/v1beta")

    [event async for event in provider.stream(_request({"effort": 0.75}))]

    body = json.loads(route.calls[0].request.content)
    assert body["effort"] == 0.75
    assert "generationConfig" not in body
