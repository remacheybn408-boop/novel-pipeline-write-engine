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


class OllamaProvider(ModelProvider):
    """Native Ollama /api/tags and /api/chat adapter for local deployments."""

    provider_id = "ollama"

    def __init__(self, api_key: str = "", base_url: str = "http://ollama:11434", timeout: httpx.Timeout = DEFAULT_HTTP_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"} if self.api_key else {"Content-Type": "application/json"}

    async def validate_credentials(self) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/api/tags", headers=self.headers)
            response.raise_for_status()
        return {"valid": True}

    async def list_models(self) -> list[ProviderModel]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/api/tags", headers=self.headers)
            response.raise_for_status()
        return [ProviderModel(self.provider_id, item["name"], item["name"], {}) for item in response.json().get("models", []) if item.get("name")]

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
            payload["options"] = {"temperature": request.temperature}
        # Reasoning payload (official think field from known_reasoning) merges
        # at the top level; None (AUTO) sends nothing extra.
        if request.reasoning is not None:
            payload.update(request.reasoning)
        async with httpx.AsyncClient(timeout=self.timeout) as client, client.stream("POST", f"{self.base_url}/api/chat", headers=self.headers, json=payload) as response:
            response.raise_for_status()
            started = False
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if not started:
                    started = True
                    yield GenerationEvent("response.started", data={"model": request.model})
                text = item.get("message", {}).get("content", "")
                if text:
                    yield GenerationEvent("content.delta", str(text))
                if item.get("done"):
                    usage = {key: item[key] for key in ("prompt_eval_count", "eval_count") if key in item}
                    if usage:
                        yield GenerationEvent("usage.updated", data=usage)
                    yield GenerationEvent("response.completed", data={"model": request.model})
