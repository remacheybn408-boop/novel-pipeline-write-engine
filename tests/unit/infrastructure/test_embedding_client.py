"""EmbeddingClient: batching, retry/backoff, truncation, error mapping.

Uses httpx.MockTransport via the client's transport injection; asyncio.sleep
is patched out so backoff tests stay instant.
"""

from __future__ import annotations

import json

import httpx
import pytest

from proseforge.infrastructure.embeddings.client import (
    MAX_INPUT_CHARS,
    EmbeddingClient,
    EmbeddingError,
    batch_limit_for,
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", _sleep)


def _client(handler, provider: str = "openai") -> EmbeddingClient:
    return EmbeddingClient(
        provider, "embed-model", "sk-test", "https://api.example.com/v1",
        transport=httpx.MockTransport(handler),
    )


def _ok_response(request: httpx.Request) -> httpx.Response:
    inputs = json.loads(request.content)["input"]
    return httpx.Response(
        200,
        json={
            "data": [{"index": i, "embedding": [float(i), 1.0]} for i in range(len(inputs))],
            "usage": {"total_tokens": len(inputs) * 7},
        },
    )


def test_batch_limit_table():
    assert batch_limit_for("volcengine") == 4
    assert batch_limit_for("dashscope") == 10
    assert batch_limit_for("siliconflow") == 32
    assert batch_limit_for("zhipu") == 64
    assert batch_limit_for("openai") == 16
    assert batch_limit_for("unknown-vendor") == 16


@pytest.mark.asyncio
async def test_embed_splits_into_provider_sized_batches():
    batch_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        batch_sizes.append(len(json.loads(request.content)["input"]))
        return _ok_response(request)

    client = _client(handler, provider="volcengine")  # limit 4
    result = await client.embed([f"text-{i}" for i in range(9)])
    assert batch_sizes == [4, 4, 1]
    assert len(result.vectors) == 9
    assert result.total_tokens == 9 * 7


@pytest.mark.asyncio
async def test_retry_on_429_then_success():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return _ok_response(request)

    result = await _client(handler).embed(["hello"])
    assert calls == 2
    assert result.vectors == [[0.0, 1.0]]


@pytest.mark.asyncio
async def test_retry_exhausted_raises_embedding_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["hello"])


@pytest.mark.asyncio
async def test_transport_error_retried_then_raises():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused")

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["hello"])
    assert calls == 3


@pytest.mark.asyncio
async def test_client_error_not_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["hello"])
    assert calls == 1


@pytest.mark.asyncio
async def test_overlong_input_truncated_and_reported():
    sent_lengths: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_lengths.extend(len(item) for item in json.loads(request.content)["input"])
        return _ok_response(request)

    long_text = "字" * (MAX_INPUT_CHARS + 500)
    result = await _client(handler).embed([long_text, "short"])
    assert sent_lengths == [MAX_INPUT_CHARS, 5]
    assert result.truncated == [0]
    assert len(result.vectors) == 2


@pytest.mark.asyncio
async def test_result_count_mismatch_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}], "usage": {}})

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["a", "b"])
