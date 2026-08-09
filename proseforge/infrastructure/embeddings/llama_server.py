"""llama.cpp-backed local embedding engine for models fastembed cannot serve.

fastembed (ONNX) covers the small default models; BGE-M3 and the Qwen3
embedding family need architectures fastembed does not support, so they run
in llama.cpp's llama-server (OpenAI-compatible /v1/embeddings), spawned on
demand from GGUF weights. Requests go through the shared EmbeddingClient.

Deployment convention (artifacts are provisioned out-of-band, e.g. via
packaging/models/fetch.py --include-gguf + fetch_llama_bin.py, the in-app
download endpoint, or baked into the Docker image at build time):

  binary:  <root>/llama-bin/llama-server
  weights: <root>/gguf/<gguf_file>

where <root> is each of BUNDLED_MODELS_ROOTS (image-baked, e.g.
/opt/proseforge/models) first, then <embedding_cache_dir> (download cache).
Bundled roots win so an offline image never re-downloads what it ships.

Cross-process coordination mirrors local.py: a lock file plus a per-model
status file for downloads, and a per-model server file (pid/port) for the
spawned llama-server — the first process to spawn writes it, later processes
(api + worker) reuse the running server instead of spawning a second one.

Neither BGE-M3 nor Qwen3 needs e5-style query/passage prefixes: embed and
embed_query send identical payloads.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from proseforge.infrastructure.embeddings.client import (
    EmbeddingClient,
    EmbeddingError,
    EmbeddingResult,
)
from proseforge.infrastructure.embeddings.local import (
    _downloading_stale,
    _hf_offline_lifted,
    _read_status_file,
    _write_status_file,
)

logger = logging.getLogger(__name__)

# Registry of llama.cpp-served models. Ports are fixed per model and bound
# to 127.0.0.1 only: at most one llama-server per model, shared by every
# process on the host. ready_timeout_s covers cold-start weight loading on
# slow CPUs (the 4B model on a 4-core box needs several minutes).
LLAMA_MODELS: dict[str, dict[str, int | str]] = {
    "BAAI/bge-m3": {
        "gguf_repo": "gpustack/bge-m3-GGUF",
        "gguf_file": "bge-m3-Q8_0.gguf",
        "dimension": 1024,
        "pooling": "cls",
        "size_mb": 700,
        "port": 8181,
        "ready_timeout_s": 120,
    },
    "Qwen/Qwen3-Embedding-0.6B": {
        "gguf_repo": "Qwen/Qwen3-Embedding-0.6B-GGUF",
        "gguf_file": "Qwen3-Embedding-0.6B-Q8_0.gguf",
        "dimension": 1024,
        "pooling": "last",
        "size_mb": 640,
        "port": 8182,
        "ready_timeout_s": 120,
    },
    "Qwen/Qwen3-Embedding-4B": {
        "gguf_repo": "Qwen/Qwen3-Embedding-4B-GGUF",
        "gguf_file": "Qwen3-Embedding-4B-Q4_K_M.gguf",
        "dimension": 2560,
        "pooling": "last",
        "size_mb": 2500,
        "port": 8183,
        "ready_timeout_s": 300,
    },
}

LLAMA_BIN_DIR = "llama-bin"
GGUF_DIR = "gguf"
_CONTEXT_TOKENS = 8192
# Physical/logical batch size: must exceed the largest single chunk in
# tokens (bge-m3 chunk_chars=1200 ≈ 900-1300 CJK tokens). llama.cpp's
# default of 512 rejects those chunks with HTTP 500.
_BATCH_TOKENS = 2048

# Offline-bundle roots: Docker images bake the GGUF weights and the
# llama-server binary here at build time (docker/*.Dockerfile), and the
# runtime looks here BEFORE the download cache — a baked-in engine keeps
# working when embedding_cache_dir is overridden to an empty data directory
# or the network is unreachable. Not a setting: a fixed deployment
# convention (settings.py stays untouched). The cache dir remains a valid
# fallback, so manually downloaded models keep working.
BUNDLED_MODELS_ROOTS: tuple[Path, ...] = (Path("/opt/proseforge/models"),)

_LOCK_STALE_SECONDS = 15 * 60
_SPAWN_LOCK_STALE_SECONDS = 10 * 60
_DOWNLOAD_TIMEOUT_SECONDS = 30 * 60
_POLL_INTERVAL_SECONDS = 2.0
_HEALTH_POLL_SECONDS = 1.0


def _model_slug(model: str) -> str:
    return model.replace("/", "--")


def gguf_dir(cache_dir: str | Path) -> Path:
    return Path(cache_dir).expanduser() / GGUF_DIR


def gguf_path(model: str, cache_dir: str | Path) -> Path:
    return gguf_dir(cache_dir) / str(LLAMA_MODELS[model]["gguf_file"])


def _search_roots(cache_dir: str | Path) -> list[Path]:
    """Bundled offline roots first, then the download cache (deduplicated)."""
    roots = list(BUNDLED_MODELS_ROOTS)
    cache_root = Path(cache_dir).expanduser()
    if cache_root not in roots:
        roots.append(cache_root)
    return roots


def find_gguf(model: str, cache_dir: str | Path) -> Path | None:
    """Locate the model's GGUF file: bundled roots first, then the cache."""
    filename = str(LLAMA_MODELS[model]["gguf_file"])
    for root in _search_roots(cache_dir):
        candidate = gguf_dir(root) / filename
        if candidate.is_file():
            return candidate
    return None


def llama_server_binary(cache_dir: str | Path) -> Path | None:
    """Discover the provisioned llama-server binary: bundled roots first,
    then the conventional cache path."""
    for root in _search_roots(cache_dir):
        bin_dir = root / LLAMA_BIN_DIR
        for name in ("llama-server", "llama-server.exe"):
            candidate = bin_dir / name
            if candidate.is_file():
                return candidate
    return None


def _server_info_path(cache_dir: str | Path, model: str) -> Path:
    return gguf_dir(cache_dir) / f"server.{_model_slug(model)}.json"


def _server_lock_path(cache_dir: str | Path, model: str) -> Path:
    return gguf_dir(cache_dir) / f"server.{_model_slug(model)}.lock"


def _server_log_path(cache_dir: str | Path, model: str) -> Path:
    return gguf_dir(cache_dir) / f"server.{_model_slug(model)}.log"


def _read_server_info(cache_dir: str | Path, model: str) -> dict[str, object] | None:
    try:
        return json.loads(_server_info_path(cache_dir, model).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_server_info(cache_dir: str | Path, model: str, *, pid: int, port: int) -> None:
    payload = {
        "pid": pid,
        "port": port,
        "model": model,
        "started_at": datetime.now(UTC).isoformat(),
    }
    target = _server_info_path(cache_dir, model)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)


def _default_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) TERMINATES the process on Windows; probe the handle
        # instead (PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE == 259).
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong(0)
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0:
                return False
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _default_health_probe(port: int) -> bool:
    try:
        import httpx

        with httpx.Client(timeout=2.0) as client:
            return client.get(f"http://127.0.0.1:{port}/health").status_code == 200
    except Exception:
        return False


def _default_models_probe(port: int) -> list[str]:
    """Model paths served by the llama-server on ``port`` ([] on any failure)."""
    try:
        import httpx

        with httpx.Client(timeout=2.0) as client:
            payload = client.get(f"http://127.0.0.1:{port}/v1/models").json()
        return [
            str(entry.get("model") or entry.get("name") or "")
            for entry in payload.get("models", [])
            if isinstance(entry, dict)
        ]
    except Exception:
        return []


def _default_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _default_spawner(argv: list[str], log_path: Path) -> subprocess.Popen[bytes]:
    # The server is shared across processes: detach it from our process group
    # so signals to the parent (Ctrl-C, worker shutdown) do not take it down.
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")  # noqa: SIM115  # handle is owned by the spawned server process
    env = None
    if os.name != "nt":
        # Prebuilt release tarballs ship shared libs next to the binary
        # (<dir>/b<tag>/); make them discoverable without system install.
        binary_dir = Path(argv[0]).parent
        lib_dirs = [str(binary_dir)]
        for candidate in sorted(binary_dir.glob("b*/")):
            if any(candidate.glob("*.so*")):
                lib_dirs.append(str(candidate))
        existing = os.environ.get("LD_LIBRARY_PATH")
        if existing:
            lib_dirs.append(existing)
        env = {**os.environ, "LD_LIBRARY_PATH": ":".join(lib_dirs)}
    return subprocess.Popen(
        argv,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=os.name != "nt",
        env=env,
    )


def llama_model_status(model: str, cache_dir: str | Path) -> dict[str, object]:
    """Status payload for the settings endpoint; never raises.

    A GGUF file at the bundled or conventional path (provisioned by
    packaging, baked into the image, or a completed download) counts as
    ready even without a status file.
    """
    info = LLAMA_MODELS.get(model, {"dimension": 0, "size_mb": 0})
    payload: dict[str, object] = {
        "status": "not_downloaded",
        "error": None,
        "progress": None,
        "model": model,
        "size_mb": info["size_mb"],
        "dimension": info["dimension"],
    }
    if model in LLAMA_MODELS and find_gguf(model, cache_dir) is not None:
        payload["status"] = "ready"
        return payload
    root = gguf_dir(cache_dir)
    status = _read_status_file(root, model)
    if status is None:
        return payload
    state = status.get("state")
    if state == "downloading" and _downloading_stale(root, model, status):
        payload["status"] = "error"
        payload["error"] = "download stalled: the downloading process likely crashed — retry the download"
        return payload
    if state == "downloading":
        payload["status"] = "downloading"
        payload["progress"] = status.get("progress")
    elif state == "error":
        payload["status"] = "error"
        payload["error"] = status.get("error")
    return payload


def _download_gguf(model: str, cache_dir: str | Path, hf_endpoint: str | None) -> None:
    """Pull the model's GGUF file into <cache_dir>/gguf via huggingface_hub."""
    info = LLAMA_MODELS[model]
    if hf_endpoint:
        os.environ.setdefault("HF_ENDPOINT", hf_endpoint)
        os.environ.setdefault("HF_HUB_ENDPOINT", hf_endpoint)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as error:
        raise EmbeddingError(
            "GGUF download requires huggingface_hub; install it with: pip install huggingface_hub"
        ) from error
    target_dir = gguf_dir(cache_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    # A ready fastembed sibling may have armed HF_HUB_OFFLINE in this process;
    # lift it for the download (restored by the context manager).
    with _hf_offline_lifted():
        hf_hub_download(
            repo_id=str(info["gguf_repo"]),
            filename=str(info["gguf_file"]),
            local_dir=str(target_dir),
        )


def _ensure_gguf_ready(model: str, cache_dir: str | Path, hf_endpoint: str | None) -> None:
    """Ensure the GGUF file exists, downloading it if needed.

    Same lock + per-model status file coordination as local.py: the lock
    holder downloads, concurrent callers poll the status until ready/error
    or timeout. A file appearing on disk (image-bundled or manually
    provisioned) counts as ready on every poll, so deployment-side
    provisioning never blocks.
    """
    root = gguf_dir(cache_dir)
    if find_gguf(model, cache_dir) is not None:
        # Best-effort bookkeeping only: the bundled root may be read-only
        # and the cache dir is not guaranteed writable either; readiness is
        # derived from the file itself, never from this status file.
        with contextlib.suppress(OSError):
            root.mkdir(parents=True, exist_ok=True)
            status = _read_status_file(root, model)
            if status is None or status.get("state") != "ready":
                _write_status_file(root, state="ready", model=model, error=None)
        return

    lock_path = root / "download.lock"
    root.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _DOWNLOAD_TIMEOUT_SECONDS
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = time.time() - lock_path.stat().st_mtime
            if age > _LOCK_STALE_SECONDS:
                logger.warning("removing stale GGUF download lock (age %.0fs)", age)
                with contextlib.suppress(OSError):
                    lock_path.unlink()
                continue
            if find_gguf(model, cache_dir) is not None:
                return
            status = _read_status_file(root, model)
            if status is not None and status.get("state") == "error":
                raise EmbeddingError(f"GGUF download failed: {status.get('error')}")
            if time.monotonic() > deadline:
                raise EmbeddingError(f"timed out waiting for GGUF download of {model}")
            time.sleep(_POLL_INTERVAL_SECONDS)
        else:
            os.close(fd)
            break

    try:
        _write_status_file(root, state="downloading", model=model, error=None)
        _download_gguf(model, cache_dir, hf_endpoint)
        if not gguf_path(model, cache_dir).is_file():
            raise EmbeddingError(f"GGUF download of {model} finished but the file is missing")
        _write_status_file(root, state="ready", model=model, error=None)
    except EmbeddingError as error:
        _write_status_file(root, state="error", model=model, error=str(error)[:500])
        raise
    except Exception as error:
        _write_status_file(root, state="error", model=model, error=str(error)[:500])
        raise EmbeddingError(f"GGUF download failed: {error}") from error
    finally:
        with contextlib.suppress(OSError):
            lock_path.unlink()


class LlamaServerEmbedder:
    """Local embedder backed by a spawned llama-server (same contract as LocalEmbedder).

    ``spawner`` / ``health_probe`` / ``pid_alive`` / ``port_in_use`` are
    injectable so unit tests never start a real process or open a socket.
    """

    def __init__(
        self,
        model: str,
        *,
        cache_dir: str | Path,
        threads: int | None = None,
        hf_endpoint: str | None = None,
        spawner: Callable[[list[str], Path], subprocess.Popen[bytes]] | None = None,
        health_probe: Callable[[int], bool] | None = None,
        pid_alive: Callable[[int], bool] | None = None,
        port_in_use: Callable[[int], bool] | None = None,
        models_probe: Callable[[int], list[str]] | None = None,
    ):
        if model not in LLAMA_MODELS:
            raise ValueError(f"unsupported llama embedding model: {model}")
        self.model = model
        self.cache_dir = Path(cache_dir).expanduser()
        self.threads = threads if threads is not None else (os.cpu_count() or 2)
        self.hf_endpoint = hf_endpoint
        self._spawner = spawner or _default_spawner
        self._health_probe = health_probe or _default_health_probe
        self._pid_alive = pid_alive or _default_pid_alive
        self._port_in_use = port_in_use or _default_port_in_use
        self._models_probe = models_probe or _default_models_probe
        self._client: EmbeddingClient | None = None
        self._process: subprocess.Popen[bytes] | None = None  # set only when WE spawned

    @property
    def identity(self) -> str:
        """Identity string written to chunks and used for 409 conflict checks."""
        return f"local/{self.model}"

    @property
    def dimension(self) -> int:
        return int(LLAMA_MODELS[self.model]["dimension"])

    @property
    def port(self) -> int:
        return int(LLAMA_MODELS[self.model]["port"])

    async def ensure_model_ready(self) -> None:
        """Ensure the GGUF weights exist (download if needed); EmbeddingError on failure."""
        await asyncio.to_thread(_ensure_gguf_ready, self.model, self.cache_dir, self.hf_endpoint)

    def _server_argv(self, binary: Path) -> list[str]:
        # ensure_model_ready ran first, so the GGUF exists somewhere in the
        # search roots; fall back to the conventional cache path for a
        # deterministic error message if it somehow vanished in between.
        model_path = find_gguf(self.model, self.cache_dir) or gguf_path(self.model, self.cache_dir)
        return [
            str(binary),
            "-m",
            str(model_path),
            "--embedding",
            "--pooling",
            str(LLAMA_MODELS[self.model]["pooling"]),
            "-c",
            str(_CONTEXT_TOKENS),
            # Physical/logical batch must cover one chunk: the registry
            # chunk_chars for bge-m3 is 1200 chars ≈ 900-1300 CJK tokens,
            # while llama.cpp's default ubatch is 512 — every oversized
            # chunk then fails with HTTP 500 ("input too large to process").
            "-b",
            str(_BATCH_TOKENS),
            "-ub",
            str(_BATCH_TOKENS),
            "-t",
            str(self.threads),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
        ]

    def _running_server_usable(self) -> bool:
        """True when a previously spawned server (any process's) is healthy."""
        info = _read_server_info(self.cache_dir, self.model)
        if info is not None and info.get("port") == self.port:
            pid = info.get("pid")
            if isinstance(pid, int) and self._pid_alive(pid) and self._health_probe(self.port):
                return True
        # Orphan adoption: a llama-server spawned outside the registry (an
        # older process whose info file was lost, a manual start) still
        # serves embeddings. Reuse it when /v1/models proves it serves THIS
        # model — otherwise spawning would hard-fail on the occupied port.
        if not self._health_probe(self.port):
            return False
        expected = gguf_path(self.model, self.cache_dir).name
        return any(name.endswith(expected) for name in self._models_probe(self.port))

    def _spawn_and_wait(self, binary: Path) -> None:
        """Spawn llama-server and poll /health until ready. Caller holds the spawn lock."""
        if self._port_in_use(self.port):
            raise EmbeddingError(
                f"port {self.port} is already in use but no llama-server is registered for "
                f"{self.model}; free the port or remove the stale occupant"
            )
        argv = self._server_argv(binary)
        logger.info("spawning llama-server for %s on port %d", self.model, self.port)
        process = self._spawner(argv, _server_log_path(self.cache_dir, self.model))
        self._process = process
        _write_server_info(self.cache_dir, self.model, pid=process.pid, port=self.port)
        timeout = float(LLAMA_MODELS[self.model]["ready_timeout_s"])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise EmbeddingError(
                    f"llama-server exited during startup (code {process.returncode}); "
                    f"see {_server_log_path(self.cache_dir, self.model)}"
                )
            if self._health_probe(self.port):
                return
            time.sleep(_HEALTH_POLL_SECONDS)
        raise EmbeddingError(f"llama-server for {self.model} not healthy within {timeout:.0f}s")

    def _ensure_server_running(self, binary: Path) -> None:
        """Reuse a healthy registered server, else spawn one under the spawn lock."""
        if self._running_server_usable():
            return
        lock_path = _server_lock_path(self.cache_dir, self.model)
        deadline = time.monotonic() + float(LLAMA_MODELS[self.model]["ready_timeout_s"]) + 60
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                age = time.time() - lock_path.stat().st_mtime
                if age > _SPAWN_LOCK_STALE_SECONDS:
                    logger.warning("removing stale llama-server spawn lock (age %.0fs)", age)
                    with contextlib.suppress(OSError):
                        lock_path.unlink()
                    continue
                # A peer is spawning; wait for it to publish a usable server.
                if self._running_server_usable():
                    return
                if time.monotonic() > deadline:
                    raise EmbeddingError(f"timed out waiting for llama-server for {self.model}")
                time.sleep(_POLL_INTERVAL_SECONDS)
            else:
                os.close(fd)
                break
        try:
            # Re-check under the lock: the previous lock holder may have
            # finished while we were acquiring.
            if self._running_server_usable():
                return
            self._spawn_and_wait(binary)
        finally:
            with contextlib.suppress(OSError):
                lock_path.unlink()

    async def ensure_ready(self) -> None:
        """Ensure weights, binary, and a running llama-server; EmbeddingError on failure."""
        if self._client is not None:
            return
        await self.ensure_model_ready()
        binary = llama_server_binary(self.cache_dir)
        if binary is None:
            searched = ", ".join(str(root / LLAMA_BIN_DIR) for root in _search_roots(self.cache_dir))
            raise EmbeddingError(
                f"llama-server binary not found (searched: {searched}); "
                "provision it manually (llama.cpp release build, see "
                "packaging/models/fetch_llama_bin.py) before using this model"
            )
        await asyncio.to_thread(self._ensure_server_running, binary)
        # /v1: llama-server's OpenAI-compatible mount. The bare /embeddings
        # endpoint answers llama.cpp-native format (a bare JSON array for
        # multi-input, nested embedding lists) which EmbeddingClient's
        # OpenAI-shaped parsing cannot read — /v1/embeddings returns proper
        # {"data": [{"embedding": [...]}], "usage": ...} with flat vectors.
        self._client = EmbeddingClient("llama", self.model, "none", f"http://127.0.0.1:{self.port}/v1")

    async def _restart_server(self) -> None:
        """Tear down our server handle and start fresh (one-shot crash recovery)."""
        if self._process is not None and self._process.poll() is None:
            with contextlib.suppress(OSError):
                self._process.terminate()
        self._process = None
        with contextlib.suppress(OSError):
            _server_info_path(self.cache_dir, self.model).unlink()
        binary = llama_server_binary(self.cache_dir)
        if binary is None:
            raise EmbeddingError("llama-server binary disappeared; cannot restart")
        await asyncio.to_thread(self._ensure_server_running, binary)

    async def _embed_with_restart(self, texts: list[str]) -> EmbeddingResult:
        assert self._client is not None  # ensure_ready ran first
        try:
            return await self._client.embed(texts)
        except EmbeddingError as first_error:
            logger.warning("llama-server request failed (%s); restarting once", first_error)
            await self._restart_server()
            try:
                return await self._client.embed(texts)
            except EmbeddingError as second_error:
                raise EmbeddingError(
                    f"llama-server embedding failed after one restart: {second_error}"
                ) from second_error

    async def _embed_texts(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=[], total_tokens=0)
        await self.ensure_ready()
        return await self._embed_with_restart(texts)

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed document texts (indexing side; same contract as EmbeddingClient.embed)."""
        return await self._embed_texts(texts)

    async def embed_query(self, texts: list[str]) -> EmbeddingResult:
        """Query side: BGE-M3/Qwen3 take no prefixes — identical to embed."""
        return await self._embed_texts(texts)
