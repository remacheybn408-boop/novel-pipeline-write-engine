"""llama-server coordination regression tests (incident follow-up).

Two bugs hit production this week; these tests pin their fixes:

1. Dual-worker orphan claiming: two workers calling ensure_ready
   concurrently must converge on ONE llama-server process — the spawn-lock
   loser waits and adopts the winner's server instead of spawning a second
   one onto the same fixed port (port conflict / mutual stealing).
2. Endpoint path: llama-server's OpenAI-compatible API lives under /v1.
   A client pointed at the bare host (missing /v1) must fail LOUDLY with a
   readable, locatable error — never a silent success with empty/garbage
   vectors (that failure mode left jobs "done" over an empty index).

Everything is faked: spawner, health/pid/port probes and the HTTP layer
(httpx.MockTransport). No real llama-server binary, no sockets, no network.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from proseforge.infrastructure.embeddings import llama_server
from proseforge.infrastructure.embeddings.client import EmbeddingClient, EmbeddingError
from proseforge.infrastructure.embeddings.llama_server import (
    LLAMA_MODELS,
    LlamaServerEmbedder,
)

_MODEL = "BAAI/bge-m3"
_PORT = int(LLAMA_MODELS[_MODEL]["port"])
_DIM = int(LLAMA_MODELS[_MODEL]["dimension"])


class _FakeProcess:
    """Minimal Popen stand-in: a pid and a controllable exit state."""

    def __init__(self, pid: int = 4321):
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class _SlowSpawner:
    """Thread-safe spawner that holds the spawn window open briefly, so a
    concurrent ensure_ready really contends on the spawn lock file. Records
    every spawn — the core assertion is that exactly one happens."""

    def __init__(self, pid: int = 4321, delay: float = 0.2):
        self._pid = pid
        self._delay = delay
        self._lock = threading.Lock()
        self.spawned = threading.Event()
        self.argv_calls: list[list[str]] = []
        self.process: _FakeProcess | None = None

    def __call__(self, argv: list[str], log_path: Path) -> _FakeProcess:
        time.sleep(self._delay)  # simulate cold-start: peer must wait, not spawn
        with self._lock:
            self.argv_calls.append(list(argv))
            self.process = _FakeProcess(pid=self._pid)
            self.spawned.set()
            return self.process


def _provision(cache_dir: Path, model: str = _MODEL) -> None:
    """Drop a fake binary + fake GGUF at the conventional provisioning paths."""
    bin_dir = cache_dir / llama_server.LLAMA_BIN_DIR
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "llama-server").write_bytes(b"fake-binary")
    target = llama_server.gguf_path(model, cache_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fake-gguf")


def _make_embedder(cache_dir: Path, spawner: _SlowSpawner, spawned: threading.Event) -> LlamaServerEmbedder:
    return LlamaServerEmbedder(
        _MODEL,
        cache_dir=cache_dir,
        spawner=spawner,
        # The server answers /health only once someone actually spawned it.
        health_probe=lambda port: spawned.is_set(),
        pid_alive=lambda pid: True,
        port_in_use=lambda port: False,
        models_probe=lambda port: [],
    )


@pytest.mark.asyncio
async def test_concurrent_workers_converge_on_single_server_no_orphans(tmp_path, monkeypatch):
    """Bug 1 shape: two workers ensure_ready at the same time. The spawn-lock
    loser must wait for the winner's published server and adopt it — one
    spawn total, one server-info file, both embedders ready on the same port,
    no port-conflict error, no orphan process."""
    # Shrink the real poll sleeps so the contended path stays fast.
    monkeypatch.setattr(llama_server, "_POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(llama_server, "_HEALTH_POLL_SECONDS", 0.02)
    _provision(tmp_path)
    spawner = _SlowSpawner()
    worker_a = _make_embedder(tmp_path, spawner, spawner.spawned)
    worker_b = _make_embedder(tmp_path, spawner, spawner.spawned)

    results = await asyncio.gather(
        worker_a.ensure_ready(), worker_b.ensure_ready(), return_exceptions=True
    )

    assert results == [None, None], f"a worker failed: {results}"
    assert len(spawner.argv_calls) == 1, "both workers spawned — port conflict / orphan"
    spawn_argv = spawner.argv_calls[0]
    assert spawn_argv[spawn_argv.index("--port") + 1] == str(_PORT)
    # Both workers ended up serving through the same single server.
    assert worker_a._process is None or worker_b._process is None  # at most one owner
    assert worker_a._client is not None and worker_b._client is not None
    assert worker_a._client.base_url == worker_b._client.base_url == f"http://127.0.0.1:{_PORT}/v1"
    # Exactly one published server record, pointing at the spawned pid.
    info_files = list(llama_server.gguf_dir(tmp_path).glob("server.*.json"))
    assert len(info_files) == 1
    info = json.loads(info_files[0].read_text(encoding="utf-8"))
    assert info["pid"] == spawner.process.pid and info["port"] == _PORT


def _llama_server_transport(calls: list[str]) -> httpx.MockTransport:
    """Emulates llama-server: OpenAI shape ONLY under /v1; every other path
    404s with the requested path in the body (so errors stay locatable)."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/embeddings":
            payload = json.loads(request.content)
            inputs = payload["input"]
            return httpx.Response(
                200,
                json={
                    "data": [{"index": index, "embedding": [0.5] * _DIM} for index in range(len(inputs))],
                    "usage": {"total_tokens": sum(len(text) for text in inputs)},
                },
            )
        return httpx.Response(404, json={"error": f"unknown endpoint: {request.url.path}"})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_embedder_requests_hit_the_v1_mount(tmp_path):
    """Positive guard of the /v1 fix: after ensure_ready, the embedder's
    client must complete a real round trip against a server that ONLY serves
    /v1 — reverting the fix (bare host base_url) makes this a 404."""
    _provision(tmp_path)
    spawner = _SlowSpawner(delay=0)
    embedder = _make_embedder(tmp_path, spawner, spawner.spawned)
    await embedder.ensure_ready()
    assert embedder._client is not None and embedder._client.base_url.endswith("/v1")

    calls: list[str] = []
    # Re-wire the same base_url through a transport that 404s off-/v1 paths.
    embedder._client = EmbeddingClient(
        "llama", _MODEL, "none", embedder._client.base_url, transport=_llama_server_transport(calls)
    )

    result = await embedder.embed(["第一章正文"])

    assert calls == ["POST /v1/embeddings"]
    assert result.vectors == [[0.5] * _DIM]


@pytest.mark.asyncio
async def test_base_url_missing_v1_fails_loudly_not_silently():
    """Bug 2 shape: a base_url missing /v1 points at paths the server does
    not serve. The embedding call must raise a readable EmbeddingError
    (HTTP status + failing path) — which execute_index_job records as the
    job error — instead of returning silent empty/garbage vectors that would
    mark a job done over an unindexed chapter."""
    calls: list[str] = []
    client = EmbeddingClient(
        "openai", "embed-1", "sk-test",
        "http://127.0.0.1:8181",  # missing /v1 — the misconfiguration
        transport=_llama_server_transport(calls),
    )

    with pytest.raises(EmbeddingError) as excinfo:
        await client.embed(["第一章正文"])

    message = str(excinfo.value)
    assert "404" in message
    assert "/embeddings" in message  # the failing path is in the error: locatable
    assert calls and calls[0].endswith("/embeddings") and "/v1/" not in calls[0]
