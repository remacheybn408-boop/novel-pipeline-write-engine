import json

import httpx
import pytest
import respx

from proseforge.domain.ports.model_provider import GenerationRequest
from proseforge.providers.baidu import BaiduProvider
from proseforge.providers.dashscope import DashScopeProvider
from proseforge.providers.deepseek import DeepSeekProvider
from proseforge.providers.kimi import KimiProvider
from proseforge.providers.minimax import MiniMaxProvider
from proseforge.providers.mistral import MistralProvider
from proseforge.providers.tencent import TencentProvider
from proseforge.providers.volcengine import VolcEngineProvider
from proseforge.providers.xai import XAIProvider
from proseforge.providers.zhipu import ZhipuProvider


@pytest.mark.parametrize(
    "factory",
    [
        BaiduProvider,
        DashScopeProvider,
        DeepSeekProvider,
        KimiProvider,
        MiniMaxProvider,
        MistralProvider,
        TencentProvider,
        VolcEngineProvider,
        XAIProvider,
        ZhipuProvider,
    ],
)
@pytest.mark.asyncio
@respx.mock
async def test_chat_provider_contract_preserves_unknown_models_and_streams(factory) -> None:
    base_url = "https://vendor.test/v1"
    models = respx.get(f"{base_url}/models").mock(return_value=httpx.Response(200, json={"data": [{"id": "future-model"}]}))
    completion = respx.post(f"{base_url}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=(
                'data: {"id":"resp-1","choices":[{"delta":{"content":"Hello"}}]}\n\n'
                'data: {"id":"resp-1","usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n'
                "data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    provider = factory("secret", base_url=base_url)
    listed = await provider.list_models()
    events = [
        event
        async for event in provider.stream(
            GenerationRequest("future-model", ({"text": "system"},), ({"role": "user", "text": "Hi"},))
        )
    ]
    assert models.called
    assert listed[0].model_id == "future-model"
    assert completion.called
    assert completion.calls[0].request.headers["authorization"] == "Bearer secret"
    body = json.loads(completion.calls[0].request.content)
    assert body["model"] == "future-model"
    assert body["messages"][-1] == {"role": "user", "content": "Hi"}
    assert [event.event for event in events] == ["response.started", "content.delta", "usage.updated", "response.completed"]
    assert events[1].text == "Hello"


@pytest.mark.asyncio
@respx.mock
async def test_chat_completions_requests_usage_and_passes_reasoning() -> None:
    base_url = "https://vendor.test/v1"
    route = respx.post(f"{base_url}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=(
                'data: {"id":"r1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
                'data: {"id":"r1","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
                "data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    provider = DeepSeekProvider("secret", base_url=base_url)
    events = [
        event
        async for event in provider.stream(
            GenerationRequest("deepseek-chat", (), ({"role": "user", "text": "Hi"},), reasoning={"reasoning_effort": "high"})
        )
    ]
    payload = json.loads(route.calls[0].request.content)
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["reasoning_effort"] == "high"
    usage_events = [event for event in events if event.event == "usage.updated"]
    assert usage_events and usage_events[0].data["final"] is True
    assert usage_events[0].data["prompt_tokens"] == 3


@pytest.mark.asyncio
@respx.mock
async def test_chat_completions_maps_response_schema_to_portable_json_object() -> None:
    # json_schema（OpenAI structured outputs）在多数 OpenAI 兼容厂商不被支持
    # （deepseek 生产实测 400）；统一降级为通用 json_object。
    base_url = "https://vendor.test/v1"
    route = respx.post(f"{base_url}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=(
                'data: {"id":"r1","choices":[{"delta":{"content":"{}"}}]}\n\n'
                "data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    provider = DeepSeekProvider("secret", base_url=base_url)
    async for _event in provider.stream(
        GenerationRequest("deepseek-chat", (), ({"role": "user", "text": "输出 JSON"},), response_schema={"type": "object"})
    ):
        pass
    payload = json.loads(route.calls[0].request.content)
    assert payload["response_format"] == {"type": "json_object"}
    assert "json_schema" not in payload.get("response_format", {})
