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


class ChatCompletionsProvider(ModelProvider):
    """Explicit OpenAI-compatible chat adapter used only by vendors that publish this contract."""

    provider_id = "compatible"
    default_base_url = ""
    auth_header = "Authorization"
    models_path = "/models"
    generation_path = "/chat/completions"

    def __init__(self, api_key: str = "", base_url: str | None = None, timeout: httpx.Timeout = DEFAULT_HTTP_TIMEOUT):
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @property
    def headers(self) -> dict[str, str]:
        value = f"Bearer {self.api_key}" if self.auth_header.lower() == "authorization" else self.api_key
        return {self.auth_header: value, "Content-Type": "application/json"}

    async def validate_credentials(self) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}{self.models_path}", headers=self.headers)
            response.raise_for_status()
        return {"valid": True}

    async def list_models(self) -> list[ProviderModel]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}{self.models_path}", headers=self.headers)
            response.raise_for_status()
        items = response.json().get("data", [])
        return [
            # display_name must be the model id; owned_by is the vendor name
            # (e.g. "deepseek") and would make every entry look identical.
            ProviderModel(self.provider_id, str(item["id"]), str(item["id"]), item.get("capabilities", {}))
            for item in items
            if item.get("id")
        ]

    async def count_tokens(self, request: GenerationRequest) -> int:
        return max(1, sum(len(str(block)) for block in (*request.system_blocks, *request.input_blocks)) // 2)

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        messages = []
        for block in request.system_blocks:
            messages.append({"role": "system", "content": str(block.get("text", block.get("content", "")))})
        for block in request.input_blocks:
            messages.append({"role": str(block.get("role", "user")), "content": str(block.get("text", block.get("content", "")))})
        # stream_options.include_usage：让 OpenAI 兼容端点在流末回 usage chunk，
        # 否则 usage 永远缺失、GenerateReply 只能记 source="missing"。
        payload: dict[str, object] = {"model": request.model, "messages": messages, "stream": True, "stream_options": {"include_usage": True}}
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        # 思考强度：catalog 参数名（如 reasoning_effort）按 chat.completions
        # 契约顶层透传；None（AUTO）不多发字段。
        if request.reasoning is not None:
            payload.update(request.reasoning)
        if request.response_schema is not None:
            # json_schema（OpenAI structured outputs）在多数 OpenAI 兼容厂商
            # 不被支持（deepseek 实测 400）；json_object 是通用子集——调用方
            # 的输出均为防御性解析，丢字段级约束无影响。
            payload["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=self.timeout) as client, client.stream("POST", f"{self.base_url}{self.generation_path}", headers=self.headers, json=payload) as response:
            response.raise_for_status()
            response_id = ""
            started = False
            finish_reason = ""
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    data: dict[str, object] = {"id": response_id}
                    if finish_reason:
                        data["finish_reason"] = finish_reason
                    yield GenerationEvent("response.completed", data=data)
                    continue
                item = json.loads(raw)
                response_id = str(item.get("id", response_id))
                if not started:
                    started = True
                    yield GenerationEvent("response.started", data={"id": response_id})
                for choice in item.get("choices", []):
                    reason = choice.get("finish_reason")
                    if reason:
                        finish_reason = str(reason)  # last non-null wins; usage chunks have no choices
                    delta = choice.get("delta", {})
                    reasoning_text = delta.get("reasoning_content") or ""
                    if reasoning_text:
                        # Reasoning stream (deepseek etc.): forwarded as-is, never part of the reply body.
                        yield GenerationEvent("reasoning.delta", str(reasoning_text), {"id": response_id})
                    text = delta.get("content", "") or ""
                    if text:
                        yield GenerationEvent("content.delta", text, {"id": response_id})
                usage = item.get("usage")
                if usage:
                    # 带 usage 的 chunk 是流末终值（include_usage 契约），标 final。
                    yield GenerationEvent("usage.updated", data={**usage, "final": True})
