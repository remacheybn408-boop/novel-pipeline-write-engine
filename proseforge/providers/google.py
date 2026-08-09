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


class GoogleProvider(ModelProvider):
    """Native Gemini Generate Content API adapter."""

    provider_id = "google"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout: httpx.Timeout = DEFAULT_HTTP_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self._headers = {"x-goog-api-key": api_key, "content-type": "application/json"}
        self.timeout = timeout

    async def validate_credentials(self) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/models", headers=self._headers)
            response.raise_for_status()
        return {"valid": True}

    async def list_models(self) -> list[ProviderModel]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/models", headers=self._headers)
            response.raise_for_status()
        return [
            ProviderModel(
                self.provider_id,
                str(item["name"]).removeprefix("models/"),
                item.get("displayName", item["name"]),
                {
                    "supported_generation_methods": item.get(
                        "supportedGenerationMethods", []
                    )
                },
            )
            for item in response.json().get("models", [])
        ]

    async def count_tokens(self, request: GenerationRequest) -> int:
        return max(
            1,
            sum(
                len(str(block.get("text", block.get("content", ""))))
                for block in (*request.system_blocks, *request.input_blocks)
            )
            // 2,
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        contents = [
            {
                "role": "model" if block.get("role") == "assistant" else "user",
                "parts": [
                    {"text": str(block.get("text", block.get("content", "")))}
                ],
            }
            for block in request.input_blocks
        ]
        payload: dict[str, object] = {"contents": contents}
        if request.system_blocks:
            payload["systemInstruction"] = {
                "parts": [
                    {
                        "text": "\n".join(
                            str(block.get("text", block.get("content", "")))
                            for block in request.system_blocks
                        )
                    }
                ]
            }
        model = request.model if request.model.startswith("models/") else f"models/{request.model}"
        # 思考强度：catalog 参数名 thinking_budget 在 Gemini API 里须放进
        # generationConfig.thinkingConfig.thinkingBudget（合并而非覆盖已有的
        # generationConfig）；未知键保持顶层透传。None（AUTO）不多发字段。
        if request.reasoning is not None:
            reasoning = dict(request.reasoning)
            thinking_budget = reasoning.pop("thinking_budget", None)
            if thinking_budget is not None:
                generation_config = payload.get("generationConfig")
                if not isinstance(generation_config, dict):
                    generation_config = {}
                    payload["generationConfig"] = generation_config
                generation_config["thinkingConfig"] = {"thinkingBudget": thinking_budget}
            payload.update(reasoning)
        url = f"{self.base_url}/{model}:streamGenerateContent?alt=sse"
        async with httpx.AsyncClient(timeout=self.timeout) as client, client.stream("POST", url, headers=self._headers, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[5:].strip())
                # usageMetadata 可能随任一 delta 返回（末帧带完整计数），
                # 提出来发 usage.updated 让 GenerateReply 上账。
                if isinstance(data.get("usageMetadata"), dict):
                    yield GenerationEvent("usage.updated", data=data)
                text = "".join(
                    str(part.get("text", ""))
                    for candidate in data.get("candidates", [])
                    for part in candidate.get("content", {}).get("parts", [])
                )
                # 仅携带 usageMetadata / 元信息的帧没有文本，不发空 content.delta。
                if text:
                    yield GenerationEvent("content.delta", text, data)
