from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from proseforge.domain.ports.model_provider import (
    GenerationEvent,
    GenerationRequest,
    ModelProvider,
    ProviderModel,
)
from proseforge.providers.http_timeout import DEFAULT_HTTP_TIMEOUT


class CohereProvider(ModelProvider):
    """Native Cohere V2 Chat adapter."""

    provider_id = "cohere"

    def __init__(self, api_key: str = "", base_url: str = "https://api.cohere.com", timeout: httpx.Timeout = DEFAULT_HTTP_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def validate_credentials(self) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/v1/models", headers=self.headers)
            response.raise_for_status()
        return {"valid": True}

    async def list_models(self) -> list[ProviderModel]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/v1/models", headers=self.headers)
            response.raise_for_status()
        return [
            ProviderModel(self.provider_id, str(item.get("name", item.get("id"))), str(item.get("name", item.get("id"))), {"features": item.get("endpoints", [])})
            for item in response.json().get("models", response.json().get("data", []))
            if item.get("name", item.get("id"))
        ]

    async def count_tokens(self, request: GenerationRequest) -> int:
        return max(1, sum(len(str(block)) for block in (*request.system_blocks, *request.input_blocks)) // 2)

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        messages = []
        for block in request.system_blocks:
            messages.append({"role": "system", "content": str(block.get("text", block.get("content", "")))})
        for block in request.input_blocks:
            messages.append({"role": str(block.get("role", "user")), "content": str(block.get("text", block.get("content", "")))})
        payload: dict[str, object] = {"model": request.model, "messages": messages, "stream": True}
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.response_schema is not None:
            payload["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=self.timeout) as client, client.stream("POST", f"{self.base_url}/v2/chat", headers=self.headers, json=payload) as response:
            response.raise_for_status()
            started = False
            async for line in response.aiter_lines():
                if not line or line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                item = json.loads(line[5:].strip())
                event_name = str(item.get("type", locals().get("event_name", "")))
                if not started:
                    started = True
                    yield GenerationEvent("response.started", data={"id": item.get("id", "")})
                if event_name == "content-delta":
                    text = item.get("delta", {}).get("message", {}).get("content", {}).get("text", "")
                    if text:
                        yield GenerationEvent("content.delta", str(text))
                elif event_name in {"message-end", "stream-end"}:
                    usage = item.get("delta", {}).get("usage") or item.get("usage")
                    if usage:
                        yield GenerationEvent("usage.updated", data=usage)
                    yield GenerationEvent("response.completed", data=item)
