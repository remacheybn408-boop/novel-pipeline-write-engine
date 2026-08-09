"""llama.cpp embedding engine: registry integrity, binary/GGUF discovery,
pid/port server coordination, whitelist routing, crash restart. No real
llama-server is spawned and nothing is downloaded — spawner, health probe,
pid/port probes and the EmbeddingClient are all fakes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from proseforge.infrastructure.embeddings import llama_server, local
from proseforge.infrastructure.embeddings.client import EmbeddingError, EmbeddingResult
from proseforge.infrastructure.embeddings.llama_server import (
    LLAMA_MODELS,
    LlamaServerEmbedder,
    llama_model_status,
    llama_server_binary,
)
from proseforge.infrastructure.embeddings.local import LocalEmbedder, local_model_status


class _FakeProcess:
    """Minimal Popen stand-in: a pid and a controllable exit state."""

    def __init__(self, pid: int = 4321):
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


class _FakeSpawner:
    """Returns a fresh _FakeProcess per spawn (unless pre-seeded), records argv."""

    def __init__(self, processes: list[_FakeProcess] | None = None):
        self.processes = list(processes or [])
        self.argv_calls: list[list[str]] = []
        self.last_process: _FakeProcess | None = None

    def __call__(self, argv: list[str], log_path: Path) -> _FakeProcess:
        self.argv_calls.append(argv)
        process = self.processes.pop(0) if self.processes else _FakeProcess(pid=4000 + len(self.argv_calls))
        self.last_process = process
        return process


def _make_embedder(tmp_path: Path, model: str = "BAAI/bge-m3", **overrides) -> LlamaServerEmbedder:
    defaults = {
        "health_probe": lambda port: True,
        "pid_alive": lambda pid: True,
        "port_in_use": lambda port: False,
        "models_probe": lambda port: [],
    }
    defaults.update(overrides)
    return LlamaServerEmbedder(model, cache_dir=tmp_path, **defaults)


def _provision(tmp_path: Path, model: str = "BAAI/bge-m3", *, binary: bool = True, gguf: bool = True) -> None:
    if binary:
        bin_dir = tmp_path / llama_server.LLAMA_BIN_DIR
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "llama-server").write_bytes(b"fake-binary")
    if gguf:
        target = llama_server.gguf_path(model, tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-gguf")


def _provision_bundled(root: Path, model: str = "BAAI/bge-m3", *, binary: bool = True, gguf: bool = True) -> None:
    """Provision artifacts into a fake image-bundled root (same layout)."""
    _provision(root, model, binary=binary, gguf=gguf)


@pytest.fixture
def bundled_root(tmp_path: Path, monkeypatch) -> Path:
    """Redirect BUNDLED_MODELS_ROOTS to a tmp dir (the real /opt path must
    never be touched by tests)."""
    root = tmp_path / "bundled"
    monkeypatch.setattr(llama_server, "BUNDLED_MODELS_ROOTS", (root,))
    return root


def test_registry_complete():
    assert set(LLAMA_MODELS) == {"BAAI/bge-m3", "Qwen/Qwen3-Embedding-0.6B", "Qwen/Qwen3-Embedding-4B"}
    ports: list[int] = []
    for info in LLAMA_MODELS.values():
        assert str(info["gguf_repo"])
        assert str(info["gguf_file"]).endswith(".gguf")
        assert int(info["dimension"]) > 0
        assert info["pooling"] in {"cls", "last"}
        assert int(info["size_mb"]) > 0
        assert int(info["ready_timeout_s"]) >= 120
        ports.append(int(info["port"]))
    assert len(set(ports)) == len(ports)  # one fixed port per model
    assert LLAMA_MODELS["BAAI/bge-m3"]["pooling"] == "cls"
    assert LLAMA_MODELS["BAAI/bge-m3"]["dimension"] == 1024
    assert LLAMA_MODELS["Qwen/Qwen3-Embedding-0.6B"]["pooling"] == "last"
    assert LLAMA_MODELS["Qwen/Qwen3-Embedding-4B"]["dimension"] == 2560
    assert LLAMA_MODELS["Qwen/Qwen3-Embedding-4B"]["ready_timeout_s"] == 300


def test_whitelist_marks_llama_backend():
    for model in LLAMA_MODELS:
        assert local.LOCAL_EMBEDDING_MODELS[model]["backend"] == "llama"
    assert "backend" not in local.LOCAL_EMBEDDING_MODELS["BAAI/bge-small-zh-v1.5"]
    assert "backend" not in local.LOCAL_EMBEDDING_MODELS["intfloat/multilingual-e5-large"]


def test_local_embedder_rejects_llama_models(tmp_path):
    for model in LLAMA_MODELS:
        with pytest.raises(ValueError, match="LlamaServerEmbedder"):
            LocalEmbedder(model, cache_dir=tmp_path)


def test_llama_embedder_rejects_unknown_model(tmp_path):
    with pytest.raises(ValueError, match="unsupported llama embedding model"):
        LlamaServerEmbedder("foo/bar", cache_dir=tmp_path)


def test_binary_discovery(tmp_path):
    assert llama_server_binary(tmp_path) is None
    bin_dir = tmp_path / llama_server.LLAMA_BIN_DIR
    bin_dir.mkdir(parents=True)
    binary = bin_dir / "llama-server"
    binary.write_bytes(b"fake")
    assert llama_server_binary(tmp_path) == binary


def test_bundled_binary_preferred_over_cache(tmp_path, bundled_root):
    _provision(tmp_path, gguf=False)
    _provision_bundled(bundled_root, gguf=False)
    bundled_binary = bundled_root / llama_server.LLAMA_BIN_DIR / "llama-server"
    assert llama_server_binary(tmp_path) == bundled_binary


def test_cache_binary_used_when_bundled_absent(tmp_path, bundled_root):
    _provision(tmp_path, gguf=False)
    cache_binary = tmp_path / llama_server.LLAMA_BIN_DIR / "llama-server"
    assert llama_server_binary(tmp_path) == cache_binary


def test_bundled_gguf_preferred_over_cache(tmp_path, bundled_root):
    _provision(tmp_path, binary=False)
    _provision_bundled(bundled_root, binary=False)
    bundled_gguf = llama_server.gguf_path("BAAI/bge-m3", bundled_root)
    assert llama_server.find_gguf("BAAI/bge-m3", tmp_path) == bundled_gguf
    assert llama_model_status("BAAI/bge-m3", tmp_path)["status"] == "ready"


def test_bundled_gguf_counts_as_ready_without_cache(tmp_path, bundled_root):
    """Empty cache dir + image-bundled GGUF: ready, no download needed."""
    _provision_bundled(bundled_root, binary=False)
    assert llama_server.find_gguf("BAAI/bge-m3", tmp_path) is not None
    assert llama_model_status("BAAI/bge-m3", tmp_path)["status"] == "ready"


@pytest.mark.asyncio
async def test_ensure_ready_uses_bundled_paths_and_never_downloads(tmp_path, bundled_root, monkeypatch):
    _provision_bundled(bundled_root)

    def _forbidden_download(model, cache_dir, hf_endpoint):
        raise AssertionError("download must not engage for a bundled model")

    monkeypatch.setattr(llama_server, "_download_gguf", _forbidden_download)
    spawner = _FakeSpawner()
    embedder = _make_embedder(tmp_path, spawner=spawner)

    await embedder.ensure_ready()

    assert len(spawner.argv_calls) == 1
    argv = spawner.argv_calls[0]
    assert argv[0] == str(bundled_root / llama_server.LLAMA_BIN_DIR / "llama-server")
    assert argv[argv.index("-m") + 1] == str(llama_server.gguf_path("BAAI/bge-m3", bundled_root))


@pytest.mark.asyncio
async def test_bundled_gguf_ready_even_when_cache_status_write_fails(tmp_path, bundled_root, monkeypatch):
    """The bundled root / cache dir may be read-only: readiness is derived
    from the GGUF file itself, never from the bookkeeping status file."""
    _provision_bundled(bundled_root)

    def _failing_write(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(llama_server, "_write_status_file", _failing_write)
    embedder = _make_embedder(tmp_path, spawner=_FakeSpawner())

    await embedder.ensure_ready()  # must not raise


def test_status_not_downloaded_then_ready_by_file(tmp_path):
    assert llama_model_status("BAAI/bge-m3", tmp_path)["status"] == "not_downloaded"
    _provision(tmp_path, binary=False)
    status = llama_model_status("BAAI/bge-m3", tmp_path)
    assert status["status"] == "ready"
    assert status["dimension"] == 1024 and status["size_mb"] == 700


def test_status_error_from_status_file(tmp_path):
    llama_server.gguf_dir(tmp_path).mkdir(parents=True)
    llama_server._write_status_file(llama_server.gguf_dir(tmp_path), state="error", model="BAAI/bge-m3", error="boom")
    status = llama_model_status("BAAI/bge-m3", tmp_path)
    assert status["status"] == "error" and status["error"] == "boom"


def test_local_model_status_dispatches_to_llama(tmp_path):
    """local_model_status must route backend=llama models to the GGUF check,
    not the fastembed ONNX snapshot check."""
    assert local_model_status("BAAI/bge-m3", tmp_path)["status"] == "not_downloaded"
    _provision(tmp_path, binary=False)
    assert local_model_status("BAAI/bge-m3", tmp_path)["status"] == "ready"


@pytest.mark.asyncio
async def test_ensure_ready_missing_binary_has_clear_error(tmp_path):
    _provision(tmp_path, binary=False)
    embedder = _make_embedder(tmp_path)

    with pytest.raises(EmbeddingError, match="llama-server binary not found"):
        await embedder.ensure_ready()


@pytest.mark.asyncio
async def test_ensure_ready_missing_gguf_downloads_or_fails(tmp_path, monkeypatch):
    """No GGUF on disk: the download path engages; a download failure
    surfaces as EmbeddingError and is recorded in the status file."""

    def _failing_download(model, cache_dir, hf_endpoint):
        raise EmbeddingError("network unreachable")

    monkeypatch.setattr(llama_server, "_download_gguf", _failing_download)
    embedder = _make_embedder(tmp_path)

    with pytest.raises(EmbeddingError, match="network unreachable"):
        await embedder.ensure_ready()

    assert llama_model_status("BAAI/bge-m3", tmp_path)["status"] == "error"


@pytest.mark.asyncio
async def test_ensure_ready_spawns_server_with_expected_argv(tmp_path):
    _provision(tmp_path)
    spawner = _FakeSpawner()
    embedder = _make_embedder(tmp_path, spawner=spawner)

    await embedder.ensure_ready()

    assert len(spawner.argv_calls) == 1
    argv = spawner.argv_calls[0]
    assert argv[0].endswith("llama-server")
    gguf = llama_server.gguf_path("BAAI/bge-m3", tmp_path)
    assert argv[argv.index("-m") + 1] == str(gguf)
    assert argv[argv.index("--pooling") + 1] == "cls"
    assert argv[argv.index("-c") + 1] == "8192"
    # Batch must cover the largest registry chunk (bge-m3 1200 chars ≈ 1k+
    # CJK tokens); llama.cpp's default 512 rejects those with HTTP 500.
    assert argv[argv.index("-b") + 1] == "2048"
    assert argv[argv.index("-ub") + 1] == "2048"
    assert argv[argv.index("--port") + 1] == "8181"
    # The server file publishes pid/port for peer processes to reuse.
    info = json.loads(llama_server._server_info_path(tmp_path, "BAAI/bge-m3").read_text(encoding="utf-8"))
    assert info == {
        "pid": spawner.last_process.pid,
        "port": 8181,
        "model": "BAAI/bge-m3",
        "started_at": info["started_at"],
    }


@pytest.mark.asyncio
async def test_running_server_is_reused_not_respawned(tmp_path):
    _provision(tmp_path)
    llama_server._write_server_info(tmp_path, "BAAI/bge-m3", pid=999, port=8181)
    spawner = _FakeSpawner()
    embedder = _make_embedder(tmp_path, spawner=spawner)

    await embedder.ensure_ready()

    assert spawner.argv_calls == []  # healthy registered server reused


@pytest.mark.asyncio
async def test_dead_server_file_triggers_respawn(tmp_path):
    _provision(tmp_path)
    llama_server._write_server_info(tmp_path, "BAAI/bge-m3", pid=999, port=8181)
    spawner = _FakeSpawner()
    embedder = _make_embedder(tmp_path, spawner=spawner, pid_alive=lambda pid: False)

    await embedder.ensure_ready()

    assert len(spawner.argv_calls) == 1


@pytest.mark.asyncio
async def test_port_conflict_without_registered_server_errors(tmp_path):
    _provision(tmp_path)
    spawner = _FakeSpawner()
    embedder = _make_embedder(tmp_path, spawner=spawner, port_in_use=lambda port: True)

    with pytest.raises(EmbeddingError, match="port 8181 is already in use"):
        await embedder.ensure_ready()

    assert spawner.argv_calls == []


@pytest.mark.asyncio
async def test_client_targets_openai_compatible_v1_mount(tmp_path):
    # llama-server's bare /embeddings answers llama.cpp-native format (bare
    # array for multi-input); EmbeddingClient expects OpenAI shape, so the
    # client must point at the /v1 mount.
    _provision(tmp_path)
    embedder = _make_embedder(tmp_path, spawner=_FakeSpawner())

    await embedder.ensure_ready()

    assert embedder._client is not None
    assert embedder._client.base_url.endswith("/v1")


@pytest.mark.asyncio
async def test_orphan_server_serving_same_model_is_adopted(tmp_path):
    # No server-info file (orphan spawned outside the registry), but the port
    # answers healthy and /v1/models reports OUR gguf — reuse, never respawn.
    _provision(tmp_path)
    spawner = _FakeSpawner()
    embedder = _make_embedder(
        tmp_path,
        spawner=spawner,
        port_in_use=lambda port: True,
        models_probe=lambda port: ["/opt/proseforge/app/packaging/models/gguf/bge-m3-Q8_0.gguf"],
    )

    await embedder.ensure_ready()

    assert spawner.argv_calls == []


@pytest.mark.asyncio
async def test_orphan_server_serving_other_model_still_errors(tmp_path):
    _provision(tmp_path)
    spawner = _FakeSpawner()
    embedder = _make_embedder(
        tmp_path,
        spawner=spawner,
        port_in_use=lambda port: True,
        models_probe=lambda port: ["/somewhere/else/Qwen3-Embedding-0.6B-Q8_0.gguf"],
    )

    with pytest.raises(EmbeddingError, match="port 8181 is already in use"):
        await embedder.ensure_ready()

    assert spawner.argv_calls == []


@pytest.mark.asyncio
async def test_spawn_exit_during_startup_errors(tmp_path):
    _provision(tmp_path)
    dying = _FakeProcess()
    dying.returncode = 1  # crashed immediately
    embedder = _make_embedder(tmp_path, spawner=_FakeSpawner(processes=[dying]))

    with pytest.raises(EmbeddingError, match="exited during startup"):
        await embedder.ensure_ready()


class _StubClient:
    """EmbeddingClient stand-in: records inputs, fails on demand."""

    def __init__(self, failures: int = 0):
        self.calls: list[list[str]] = []
        self.failures = failures

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append(list(texts))
        if self.failures > 0:
            self.failures -= 1
            raise EmbeddingError("server down")
        return EmbeddingResult(vectors=[[1.0, 2.0] for _ in texts], total_tokens=len(texts))


async def _ready_embedder(tmp_path: Path, client: _StubClient, **overrides) -> LlamaServerEmbedder:
    _provision(tmp_path)
    spawner = _FakeSpawner()
    overrides.setdefault("spawner", spawner)
    embedder = _make_embedder(tmp_path, **overrides)
    await embedder.ensure_ready()
    embedder._client = client  # type: ignore[assignment]
    return embedder


@pytest.mark.asyncio
async def test_embed_and_query_send_identical_payloads(tmp_path):
    """BGE-M3/Qwen3 need no e5-style prefixes: both sides send raw text."""
    client = _StubClient()
    embedder = await _ready_embedder(tmp_path, client)

    document_result = await embedder.embed(["正文"])
    query_result = await embedder.embed_query(["主角是谁"])

    assert client.calls == [["正文"], ["主角是谁"]]
    assert document_result.vectors == [[1.0, 2.0]]
    assert query_result.vectors == [[1.0, 2.0]]


@pytest.mark.asyncio
async def test_empty_input_short_circuits(tmp_path):
    embedder = _make_embedder(tmp_path)
    result = await embedder.embed([])
    assert result.vectors == []


@pytest.mark.asyncio
async def test_crash_restarts_server_once_and_recovers(tmp_path):
    client = _StubClient(failures=1)
    embedder = await _ready_embedder(tmp_path, client)

    result = await embedder.embed(["重试"])

    assert result.vectors == [[1.0, 2.0]]
    # The restart tore down our spawn and spawned a replacement.
    assert len(embedder._spawner.argv_calls) == 2
    assert embedder._process is embedder._spawner.last_process


@pytest.mark.asyncio
async def test_second_failure_after_restart_raises(tmp_path):
    client = _StubClient(failures=2)
    embedder = await _ready_embedder(tmp_path, client)

    with pytest.raises(EmbeddingError, match="after one restart"):
        await embedder.embed(["重试"])

    assert len(embedder._spawner.argv_calls) == 2  # exactly one restart
