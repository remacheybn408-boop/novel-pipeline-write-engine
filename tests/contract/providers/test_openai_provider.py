import json

import httpx
import pytest
import respx

from proseforge.domain.ports.model_provider import GenerationRequest
from proseforge.providers.openai import OpenAIProvider


@pytest.mark.asyncio
@respx.mock
async def test_openai_models_preserve_unknown_ids():
    respx.get("https://api.test/v1/models").mock(return_value=httpx.Response(200, json={"data": [{"id": "future-model"}]}))
    models = await OpenAIProvider("secret", "https://api.test/v1").list_models()
    assert models[0].model_id == "future-model"


@pytest.mark.asyncio
@respx.mock
async def test_openai_response_stream_normalizes_events():
    body = "\n".join([
        "data: " + json.dumps({"type": "response.created", "response": {"id": "r1"}}),
        "data: " + json.dumps({"type": "response.output_text.delta", "delta": "Hi"}),
        "data: " + json.dumps({"type": "response.completed"}),
        "data: [DONE]",
        "",
    ])
    respx.post("https://api.test/v1/responses").mock(return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
    provider = OpenAIProvider("secret", "https://api.test/v1")
    events = [event async for event in provider.stream(GenerationRequest("future-model", (), ({"type": "message", "role": "user", "content": "Hi"},)))]
    assert [event.event for event in events] == ["response.started", "content.delta", "response.completed"]


@pytest.mark.asyncio
@respx.mock
async def test_openai_stream_maps_blocks_to_responses_api_shape():
    route = respx.post("https://api.test/v1/responses").mock(
        return_value=httpx.Response(200, text="data: " + json.dumps({"type": "response.completed"}) + "\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})
    )
    provider = OpenAIProvider("secret", "https://api.test/v1")
    request = GenerationRequest(
        "gpt-4.1-mini",
        ({"type": "text", "text": "Be terse."},),
        ({"role": "user", "text": "Hi", "source_id": "s1", "token_estimate": 3},),
    )
    [event async for event in provider.stream(request)]
    payload = json.loads(route.calls[0].request.content)
    assert payload["instructions"] == "Be terse."
    assert payload["input"] == [{"role": "user", "content": "Hi"}]


@pytest.mark.asyncio
@respx.mock
async def test_openai_unknown_events_stay_neutral_not_failed():
    body = "\n".join([
        "data: " + json.dumps({"type": "response.in_progress", "response": {"id": "r1"}}),
        "data: " + json.dumps({"type": "response.output_item.added", "item": {}}),
        "data: " + json.dumps({"type": "response.failed", "response": {"error": {"message": "boom"}}}),
        "data: [DONE]",
        "",
    ])
    respx.post("https://api.test/v1/responses").mock(return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
    provider = OpenAIProvider("secret", "https://api.test/v1")
    events = [event async for event in provider.stream(GenerationRequest("m", (), ({"role": "user", "text": "Hi"},)))]
    assert [event.event for event in events] == ["response.in_progress", "response.output_item.added", "response.failed"]


@pytest.mark.asyncio
@respx.mock
async def test_openai_completed_event_promotes_nested_usage():
    body = "\n".join([
        "data: " + json.dumps({"type": "response.completed", "response": {"id": "r1", "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}}),
        "data: [DONE]",
        "",
    ])
    respx.post("https://api.test/v1/responses").mock(return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
    provider = OpenAIProvider("secret", "https://api.test/v1")
    events = [event async for event in provider.stream(GenerationRequest("m", (), ({"role": "user", "text": "Hi"},)))]
    assert events[0].event == "response.completed"
    assert events[0].data["usage"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


@pytest.mark.asyncio
@respx.mock
async def test_openai_json_schema_format_includes_required_name():
    route = respx.post("https://api.test/v1/responses").mock(
        return_value=httpx.Response(200, text="data: " + json.dumps({"type": "response.completed"}) + "\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})
    )
    provider = OpenAIProvider("secret", "https://api.test/v1")
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}
    request = GenerationRequest("future-model", (), ({"role": "user", "text": "Hi"},), response_schema=schema)
    [event async for event in provider.stream(request)]
    payload = json.loads(route.calls[0].request.content)
    fmt = payload["text"]["format"]
    # Responses API json_schema 契约要求 name；缺 name 会被部分兼容网关 400
    assert fmt["type"] == "json_schema"
    assert fmt["name"] == "proseforge_output"
    assert fmt["schema"] == schema
    assert fmt["strict"] is False


@pytest.mark.asyncio
@respx.mock
async def test_openai_json_schema_name_prefers_schema_title():
    route = respx.post("https://api.test/v1/responses").mock(
        return_value=httpx.Response(200, text="data: " + json.dumps({"type": "response.completed"}) + "\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})
    )
    provider = OpenAIProvider("secret", "https://api.test/v1")
    schema = {"title": "chapter_plan", "type": "object", "properties": {}}
    request = GenerationRequest("future-model", (), ({"role": "user", "text": "Hi"},), response_schema=schema)
    [event async for event in provider.stream(request)]
    payload = json.loads(route.calls[0].request.content)
    assert payload["text"]["format"]["name"] == "chapter_plan"


def test_openai_default_timeout_is_long_read():
    timeout = OpenAIProvider("secret", "https://api.test/v1").timeout
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 300.0
    assert timeout.connect == 10.0
