from __future__ import annotations

import httpx

from .chat_completions import ChatCompletionsProvider
from .http_timeout import DEFAULT_HTTP_TIMEOUT


class VLLMProvider(ChatCompletionsProvider):
    provider_id = "vllm"

    def __init__(self, api_key: str = "", base_url: str = "http://vllm:8000", timeout: httpx.Timeout = DEFAULT_HTTP_TIMEOUT):
        base = base_url.rstrip("/")
        # base_url 已带 /v1 后缀时不再重复拼接。
        super().__init__(api_key, base if base.endswith("/v1") else f"{base}/v1", timeout)
