"""OpenAI-compatible embedding client.

One client for every vendor: POST {base_url}/embeddings with
``{"model": ..., "input": [...]}``. The only per-vendor difference is the
max inputs per request (volcengine 4, dashscope 10, siliconflow 32,
zhipu 64, default 16). Retries 429/5xx and transport errors with
exponential backoff; other 4xx fail immediately. Overlong inputs are
truncated by characters (conservative proxy for the token limit) and
logged — a hard failure would stall the whole indexing job for one chunk.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

_BATCH_LIMITS: dict[str, int] = {
    "volcengine": 4,
    "dashscope": 10,
    "siliconflow": 32,
    "zhipu": 64,
}
_DEFAULT_BATCH_LIMIT = 16

# Conservative char cap per input: embedding endpoints typically allow
# ~8k tokens; CJK chars are ~1 token each, English ~4 chars/token, so
# 6000 chars stays safely under either bound.
MAX_INPUT_CHARS = 6000

_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


class EmbeddingError(Exception):
    """Non-retryable embedding failure (auth, bad request, exhausted retries)."""


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    total_tokens: int = 0
    truncated: list[int] = field(default_factory=list)  # indexes of truncated inputs


def batch_limit_for(provider: str) -> int:
    return _BATCH_LIMITS.get(provider, _DEFAULT_BATCH_LIMIT)


class EmbeddingClient:
    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        base_url: str,
        *,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ):
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _truncate(self, texts: list[str]) -> tuple[list[str], list[int]]:
        truncated: list[int] = []
        clipped: list[str] = []
        for index, text in enumerate(texts):
            if len(text) > MAX_INPUT_CHARS:
                truncated.append(index)
                logger.warning(
                    "embedding input %d truncated from %d to %d chars", index, len(text), MAX_INPUT_CHARS
                )
                clipped.append(text[:MAX_INPUT_CHARS])
            else:
                clipped.append(text)
        return clipped, truncated

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed all texts, splitting into provider-sized batches."""
        vectors: list[list[float]] = []
        total_tokens = 0
        all_truncated: list[int] = []
        limit = batch_limit_for(self.provider)
        for start in range(0, len(texts), limit):
            batch = texts[start : start + limit]
            clipped, truncated = self._truncate(batch)
            all_truncated.extend(start + index for index in truncated)
            batch_vectors, batch_tokens = await self._embed_batch(clipped)
            vectors.extend(batch_vectors)
            total_tokens += batch_tokens
        return EmbeddingResult(vectors=vectors, total_tokens=total_tokens, truncated=all_truncated)

    async def embed_query(self, texts: list[str]) -> EmbeddingResult:
        """Query-side embedding (retrieval). Remote vendors take raw text for
        both sides — no e5-style prefixes — so this is the same as embed."""
        return await self.embed(texts)

    async def _embed_batch(self, batch: list[str]) -> tuple[list[list[float]], int]:
        payload = {"model": self.model, "input": batch}
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                    response = await client.post(
                        f"{self.base_url}/embeddings", headers=self.headers, json=payload
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = EmbeddingError(f"embedding HTTP {response.status_code}")
                    if attempt < _MAX_ATTEMPTS:
                        await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                        continue
                    raise last_error
                if response.status_code >= 400:
                    raise EmbeddingError(
                        f"embedding HTTP {response.status_code}: {response.text[:200]}"
                    )
                body = response.json()
                if not isinstance(body, dict):
                    # e.g. llama.cpp native /embeddings (missing /v1 mount)
                    # returns a bare JSON array — fail readably, not AttributeError.
                    raise EmbeddingError(
                        f"embedding unexpected response body (expected object with 'data', got {type(body).__name__}): {str(body)[:200]}"
                    )
                data = sorted(body.get("data", []), key=lambda item: item.get("index", 0))
                vectors = [list(item["embedding"]) for item in data]
                if len(vectors) != len(batch):
                    raise EmbeddingError(
                        f"embedding count mismatch: sent {len(batch)}, got {len(vectors)}"
                    )
                usage = body.get("usage") or {}
                return vectors, int(usage.get("total_tokens", 0))
            except httpx.TransportError as error:
                last_error = error
                if attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                    continue
                raise EmbeddingError(f"embedding transport error: {error}") from error
        raise EmbeddingError(f"embedding failed: {last_error}")
