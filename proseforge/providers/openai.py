from __future__ import annotations

# Official docs: https://platform.openai.com/docs/api-reference
# Verified: 2026-07-15
# Model discovery: Models API
# Primary generation API: Responses API
import json
from collections.abc import AsyncIterator

import httpx

from proseforge.domain.ports.model_provider import (
    GenerationEvent,
    GenerationRequest,
    ModelProvider,
    ProviderModel,
)
from proseforge.providers.events import GenerationEventType
from proseforge.providers.http_timeout import DEFAULT_HTTP_TIMEOUT


class OpenAIProvider(ModelProvider):
    provider_id = "openai"

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1", timeout: httpx.Timeout = DEFAULT_HTTP_TIMEOUT):
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def validate_credentials(self) -> dict[str, object]:
        async with self._client() as client:
            response = await client.get(f"{self.base_url}/models", headers=self._headers)
            response.raise_for_status()
            return {"valid": True}

    async def list_models(self) -> list[ProviderModel]:
        async with self._client() as client:
            response = await client.get(f"{self.base_url}/models", headers=self._headers)
            response.raise_for_status()
            return [ProviderModel("openai", item["id"], item.get("id", ""), {}) for item in response.json().get("data", [])]

    async def count_tokens(self, request: GenerationRequest) -> int:
        text = " ".join(str(block.get("text", "")) for block in (*request.system_blocks, *request.input_blocks))
        return max(1, len(text) // 2)

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        # Responses API 契约：system blocks → instructions（拼接 text），
        # input blocks → {"role", "content"} 消息列表；内部 block dict 原样发会 400。
        payload: dict[str, object] = {
            "model": request.model,
            "stream": True,
            "input": [
                {"role": str(block.get("role", "user")), "content": str(block.get("text", block.get("content", "")))}
                for block in request.input_blocks
            ],
        }
        instructions = "\n".join(str(block.get("text", block.get("content", ""))) for block in request.system_blocks).strip()
        if instructions:
            payload["instructions"] = instructions
        for field in ("temperature", "top_p", "max_output_tokens"):
            value = getattr(request, field)
            if value is not None:
                payload[field] = value
        # 思考强度：catalog 参数名 reasoning_effort 在 Responses API 里须放进
        # 嵌套的 reasoning.effort；未知键保持顶层透传。None（AUTO）不多发字段。
        if request.reasoning is not None:
            reasoning = dict(request.reasoning)
            effort = reasoning.pop("reasoning_effort", None)
            if effort is not None:
                payload["reasoning"] = {"effort": effort}
            payload.update(reasoning)
        if request.response_schema is not None:
            # Responses API json_schema 契约要求 name 字段；部分 OpenAI 兼容网关
            # （opencode zen/go 实测）缺 name 直接 400 "missing field `name`"。
            # strict=False：内部 schema 未按 strict 合规（全 required + 无可选），
            # 调用方输出均为防御性解析，丢字段级约束无影响。
            schema_name = str(request.response_schema.get("title") or "proseforge_output")
            payload["text"] = {"format": {"type": "json_schema", "name": schema_name, "schema": request.response_schema, "strict": False}}
        async with self._client() as client, client.stream("POST", f"{self.base_url}/responses", headers=self._headers, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                event = json.loads(raw)
                yield self._normalize(event)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout)

    @staticmethod
    def _normalize(event: dict[str, object]) -> GenerationEvent:
        event_type = str(event.get("type", ""))
        mapping = {
            "response.created": GenerationEventType.RESPONSE_STARTED,
            "response.output_text.delta": GenerationEventType.CONTENT_DELTA,
            "response.completed": GenerationEventType.RESPONSE_COMPLETED,
            "response.done": GenerationEventType.RESPONSE_COMPLETED,
            "response.failed": GenerationEventType.RESPONSE_FAILED,
            "response.incomplete": GenerationEventType.RESPONSE_FAILED,
            "error": GenerationEventType.RESPONSE_FAILED,
            "response.usage.updated": GenerationEventType.USAGE_UPDATED,
        }
        # 未知事件（response.in_progress / output_item.added 等）保留原始类型名，
        # 下游按不认识的事件忽略——绝不映射成 failed 误杀正常流。
        normalized = mapping.get(event_type, event_type or "provider.event")
        text = str(event.get("delta", ""))
        data = {key: value for key, value in event.items() if key not in {"type", "delta"}}
        # response.completed/failed 的 usage 嵌在 response 对象里，提升到顶层，
        # 让 GenerateReply 的 response.completed usage 路径与 normalize 都能读到。
        response_obj = event.get("response")
        if isinstance(response_obj, dict) and isinstance(response_obj.get("usage"), dict):
            data["usage"] = response_obj["usage"]
        return GenerationEvent(str(normalized), text=text, data=data)
