import json

import httpx
import pytest
import respx

from proseforge.domain.ports.model_provider import GenerationRequest
from proseforge.providers.anthropic import AnthropicProvider
from proseforge.providers.google import GoogleProvider


@pytest.mark.asyncio
@respx.mock
async def test_anthropic_uses_messages_api_headers_and_sse() -> None:
    models = respx.get("https://anthropic.test/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "claude-test", "display_name": "Claude Test"}]},
        )
    )
    messages = respx.post("https://anthropic.test/v1/messages").mock(
        return_value=httpx.Response(
            200,
            content=(
                'data: {"type":"content_block_delta","delta":{"text":"Hello"}}\n\n'
                'data: {"type":"message_stop"}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    provider = AnthropicProvider("anthropic-secret", base_url="https://anthropic.test/v1")

    listed = await provider.list_models()
    events = [
        event
        async for event in provider.stream(
            GenerationRequest(
                "claude-test",
                ({"text": "Be concise"},),
                ({"role": "user", "text": "Hi"},),
                max_output_tokens=123,
            )
        )
    ]

    assert models.called
    assert models.calls[0].request.headers["x-api-key"] == "anthropic-secret"
    assert models.calls[0].request.headers["anthropic-version"] == "2023-06-01"
    assert listed[0].model_id == "claude-test"
    assert messages.called
    body = json.loads(messages.calls[0].request.content)
    assert body["max_tokens"] == 123
    assert body["system"] == "Be concise"
    assert events[0].text == "Hello"


@pytest.mark.asyncio
@respx.mock
async def test_google_uses_gemini_streaming_endpoint_and_api_key_header() -> None:
    models = respx.get("https://google.test/v1beta/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-test",
                        "displayName": "Gemini Test",
                        "supportedGenerationMethods": ["generateContent"],
                    }
                ]
            },
        )
    )
    stream = respx.post(
        "https://google.test/v1beta/models/gemini-test:streamGenerateContent?alt=sse"
    ).mock(
        return_value=httpx.Response(
            200,
            content=(
                'data: {"candidates":[{"content":{"parts":[{"text":"Hi"}]}}]}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    provider = GoogleProvider("google-secret", base_url="https://google.test/v1beta")

    listed = await provider.list_models()
    events = [
        event
        async for event in provider.stream(
            GenerationRequest(
                "gemini-test",
                ({"text": "Use plain language"},),
                ({"role": "user", "text": "Hi"},),
            )
        )
    ]

    assert models.calls[0].request.headers["x-goog-api-key"] == "google-secret"
    assert listed[0].model_id == "gemini-test"
    assert stream.called
    body = json.loads(stream.calls[0].request.content)
    assert body["systemInstruction"]["parts"][0]["text"] == "Use plain language"
    assert body["contents"][0]["parts"][0]["text"] == "Hi"
    assert events[0].text == "Hi"


@pytest.mark.asyncio
@respx.mock
async def test_anthropic_stream_extracts_usage_events() -> None:
    respx.post("https://anthropic.test/v1/messages").mock(
        return_value=httpx.Response(
            200,
            content=(
                'data: {"type":"message_start","message":{"id":"m1","usage":{"input_tokens":12,"output_tokens":1}}}\n\n'
                'data: {"type":"content_block_delta","delta":{"text":"Hello"}}\n\n'
                'data: {"type":"message_delta","usage":{"output_tokens":7}}\n\n'
                'data: {"type":"message_stop"}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    provider = AnthropicProvider("anthropic-secret", base_url="https://anthropic.test/v1")
    events = [
        event
        async for event in provider.stream(
            GenerationRequest("claude-test", (), ({"role": "user", "text": "Hi"},))
        )
    ]
    usage_events = [event for event in events if event.event == "usage.updated"]
    assert len(usage_events) == 2
    assert usage_events[0].data["input_tokens"] == 12
    assert usage_events[0].data["final"] is False
    assert usage_events[1].data["output_tokens"] == 7
    assert usage_events[1].data["final"] is True


@pytest.mark.asyncio
@respx.mock
async def test_google_stream_emits_usage_and_skips_empty_deltas() -> None:
    respx.post("https://google.test/v1beta/models/gemini-test:streamGenerateContent?alt=sse").mock(
        return_value=httpx.Response(
            200,
            content=(
                'data: {"candidates":[{"content":{"parts":[{"text":"Hi"}]}}]}\n\n'
                'data: {"usageMetadata":{"promptTokenCount":9,"candidatesTokenCount":4,"totalTokenCount":13}}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    provider = GoogleProvider("google-secret", base_url="https://google.test/v1beta")
    events = [
        event
        async for event in provider.stream(
            GenerationRequest("gemini-test", (), ({"role": "user", "text": "Hi"},))
        )
    ]
    # 纯 usageMetadata 帧不产生空 content.delta
    assert [event.event for event in events] == ["content.delta", "usage.updated"]
    assert events[1].data["usageMetadata"]["promptTokenCount"] == 9
