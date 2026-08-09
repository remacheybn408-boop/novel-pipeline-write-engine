import json

import httpx
import pytest
import respx

from proseforge.domain.ports.model_provider import GenerationRequest
from proseforge.providers.cohere import CohereProvider


@pytest.mark.asyncio
@respx.mock
async def test_cohere_native_v2_models_and_event_sse() -> None:
    models = respx.get("https://cohere.test/v1/models").mock(return_value=httpx.Response(200, json={"models": [{"name": "command-future"}]}))
    chat = respx.post("https://cohere.test/v2/chat").mock(
        return_value=httpx.Response(
            200,
            content=(
                'event: message-start\ndata: {"type":"message-start","id":"cohere-1"}\n\n'
                'event: content-delta\ndata: {"type":"content-delta","delta":{"message":{"content":{"text":"Hello"}}}}\n\n'
                'event: message-end\ndata: {"type":"message-end","delta":{"usage":{"tokens":{"output_tokens":1}}}}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    provider = CohereProvider("secret", base_url="https://cohere.test")
    listed = await provider.list_models()
    events = [event async for event in provider.stream(GenerationRequest("command-future", (), ({"role": "user", "text": "Hi"},)))]
    assert models.called
    assert listed[0].model_id == "command-future"
    assert chat.called
    assert json.loads(chat.calls[0].request.content)["messages"][-1]["content"] == "Hi"
    assert events[1].text == "Hello"
    assert events[-1].event == "response.completed"
