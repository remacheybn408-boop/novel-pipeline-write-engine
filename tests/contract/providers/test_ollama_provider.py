import json

import httpx
import pytest
import respx

from proseforge.domain.ports.model_provider import GenerationRequest
from proseforge.providers.ollama import OllamaProvider


@pytest.mark.asyncio
@respx.mock
async def test_ollama_native_tags_and_ndjson_chat() -> None:
    tags = respx.get("http://ollama.test/api/tags").mock(return_value=httpx.Response(200, json={"models": [{"name": "future-local"}]}))
    chat = respx.post("http://ollama.test/api/chat").mock(
        return_value=httpx.Response(
            200,
            content='{"message":{"content":"Hi"}}\n{"message":{"content":" there"},"done":true,"eval_count":2}\n',
            headers={"content-type": "application/x-ndjson"},
        )
    )
    provider = OllamaProvider(base_url="http://ollama.test")
    listed = await provider.list_models()
    events = [event async for event in provider.stream(GenerationRequest("future-local", (), ({"role": "user", "text": "Hello"},)))]
    assert tags.called
    assert listed[0].model_id == "future-local"
    assert chat.called
    assert json.loads(chat.calls[0].request.content)["messages"][-1]["content"] == "Hello"
    assert "Hi" in "".join(event.text for event in events)
    assert events[-1].event == "response.completed"
