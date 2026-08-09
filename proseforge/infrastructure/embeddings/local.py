"""Local ONNX embedding engine (fastembed) for narrative RAG.

Runs embedding on CPU via Qdrant's fastembed library — no vendor API, no
torch. The registry also lists llama.cpp GGUF models (backend="llama",
served by llama_server.py); the settings UI exposes only the visible
entries (bge-m3 convergence), while hidden entries stay loadable for
rollback. Per-model indexing windows live in the registry's chunk_chars;
the 512-token fastembed models additionally truncate inputs to ~480 chars
(CJK chars are ~1 token).

Model download is coordinated across processes with a lock file and a
status file inside the cache dir: the lock holder downloads, everyone
else polls the status until ready/error/timeout. A lock older than
LOCK_STALE_SECONDS belongs to a crashed process and is force-removed.

e5 prefix note (verified against fastembed 0.8.0 source): fastembed does
NOT add the e5 "passage:"/"query:" prefixes automatically —
``intfloat/multilingual-e5-large`` is served by ``PooledEmbedding``, whose
``passage_embed`` inherits the no-op base implementation even though the
model card marks prefixes as necessary. This module therefore prepends the
prefixes itself for e5-family models: "passage: " on the indexing side
(``embed``) and "query: " on the retrieval side (``embed_query``).
bge-small-zh needs no prefix on either side.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from proseforge.infrastructure.embeddings.client import EmbeddingError, EmbeddingResult

logger = logging.getLogger(__name__)

# Whitelisted local models: name -> dimension / approximate download size.
# hf_source is the repo fastembed actually downloads from: it does NOT pull
# the original vendor repos — it uses Qdrant's repackaged ONNX exports
# (verified via TextEmbedding.list_supported_models()): bge-small-zh comes
# from Qdrant/bge-small-zh-v1.5 (model_optimized.onnx), e5-large from
# qdrant/multilingual-e5-large-onnx (model.onnx + model.onnx_data). Cache
# detection and packaging/models/fetch.py must use these repo ids.
#
# visible: only visible models are offered in the settings UI (bge-m3
# convergence); hidden entries stay fully functional — packaging fetch,
# stored preferences and the PUT whitelist still accept them, so rolling
# back is a config change, not a code change.
# chunk_chars: narrative indexing window for this model (第 15 项 按模型定窗).
# The 512-token fastembed models keep the historic 450-char window; the
# llama.cpp GGUF models run with an 8k server context, so they get ~1200.
LOCAL_EMBEDDING_MODELS: dict[str, dict[str, int | str | bool]] = {
    "BAAI/bge-small-zh-v1.5": {
        "dimension": 512, "size_mb": 90, "hf_source": "Qdrant/bge-small-zh-v1.5",
        "visible": False, "chunk_chars": 450,
    },
    "intfloat/multilingual-e5-large": {
        "dimension": 1024, "size_mb": 2100, "hf_source": "qdrant/multilingual-e5-large-onnx",
        "visible": False, "chunk_chars": 450,
    },
    # backend="llama": served by llama.cpp's llama-server from GGUF weights
    # (see infrastructure/embeddings/llama_server.py) — fastembed does not
    # support these architectures. hf_source is the GGUF repo here; details
    # (gguf_file, pooling, port) live in llama_server.LLAMA_MODELS.
    "BAAI/bge-m3": {
        "dimension": 1024, "size_mb": 700, "hf_source": "gpustack/bge-m3-GGUF", "backend": "llama",
        "visible": True, "chunk_chars": 1200,
    },
    "Qwen/Qwen3-Embedding-0.6B": {
        "dimension": 1024, "size_mb": 640, "hf_source": "Qwen/Qwen3-Embedding-0.6B-GGUF", "backend": "llama",
        "visible": False, "chunk_chars": 1200,
    },
    "Qwen/Qwen3-Embedding-4B": {
        "dimension": 2560, "size_mb": 2500, "hf_source": "Qwen/Qwen3-Embedding-4B-GGUF", "backend": "llama",
        "visible": False, "chunk_chars": 1200,
    },
}
DEFAULT_LOCAL_MODEL = "BAAI/bge-m3"
# Fallback indexing window for a model missing from the registry (the 512-token
# era default; registry entries all carry an explicit chunk_chars).
DEFAULT_LOCAL_CHUNK_CHARS = 450


def visible_local_models() -> dict[str, dict[str, int | str | bool]]:
    """Registry entries offered in the settings UI (bge-m3 only today)."""
    return {name: info for name, info in LOCAL_EMBEDDING_MODELS.items() if info.get("visible", True)}


def local_model_chunk_chars(model: str) -> int:
    """Narrative indexing window for a local model, from the registry."""
    return int(LOCAL_EMBEDDING_MODELS.get(model, {}).get("chunk_chars", DEFAULT_LOCAL_CHUNK_CHARS))

# Both whitelisted models cap at 512 tokens; CJK chars are ~1 token each.
LOCAL_MAX_INPUT_CHARS = 480

_LOCK_NAME = "download.lock"
_STATUS_NAME = "status.json"  # legacy single status file (pre per-model split)
_LOCK_STALE_SECONDS = 15 * 60
_DOWNLOAD_TIMEOUT_SECONDS = 10 * 60
# A "downloading" status file is refreshed every few seconds by the progress
# sampler while any downloader/waiter lives; silence beyond this threshold
# means the downloader crashed and the state would otherwise stick forever
# (the settings endpoint would keep reporting "downloading" and the
# frontend would poll endlessly).
_DOWNLOAD_STALE_SECONDS = 30 * 60
_POLL_INTERVAL_SECONDS = 2.0


def _model_slug(model: str) -> str:
    return model.replace("/", "--")


def _status_path(cache_dir: Path, model: str) -> Path:
    return cache_dir / f"status.{_model_slug(model)}.json"


def _model_snapshot_ready(model: str, cache_dir: Path) -> bool:
    """True when the HF hub cache under cache_dir already holds the model's
    ONNX files — either bundled by packaging/models/fetch.py (offline
    distribution) or left over from a previous download. The cache directory
    is named after fastembed's actual source repo (Qdrant's repackaged
    export, see LOCAL_EMBEDDING_MODELS), not the vendor repo id."""
    hf_source = str(LOCAL_EMBEDDING_MODELS.get(model, {}).get("hf_source", model))
    snapshot_root = cache_dir / f"models--{hf_source.replace('/', '--')}" / "snapshots"
    return any(snapshot_root.glob("*/**/*.onnx"))


def _read_status_file(cache_dir: Path, model: str) -> dict[str, object] | None:
    """Status for one model. Per-model files (status.{slug}.json) take
    precedence; installs predating the split only have the single status.json,
    which counts only when it names this model."""
    try:
        return json.loads(_status_path(cache_dir, model).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        legacy = json.loads((cache_dir / _STATUS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return legacy if legacy.get("model") == model else None


def _write_status_file(
    cache_dir: Path, *, state: str, model: str, error: str | None, progress: float | None = None
) -> None:
    payload: dict[str, object] = {
        "state": state,
        "model": model,
        "error": error,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if progress is not None:
        payload["progress"] = progress
    # Atomic publish: readers never see a half-written status file.
    status_path = _status_path(cache_dir, model)
    temporary = cache_dir / f"{status_path.name}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, status_path)


def _model_cache_bytes(model: str, cache_dir: Path) -> int:
    """Total bytes downloaded so far for the model's hub cache directory."""
    hf_source = str(LOCAL_EMBEDDING_MODELS.get(model, {}).get("hf_source", model))
    root = cache_dir / f"models--{hf_source.replace('/', '--')}"
    if not root.is_dir():
        return 0
    return sum(entry.stat().st_size for entry in root.rglob("*") if entry.is_file())


def _write_download_progress(model: str, cache_dir: Path, expected_bytes: int) -> bool:
    """Publish byte progress into the model's status file. Returns False once
    the download left the "downloading" state (or failed to start), telling
    the sampler to stop; never raises — progress is best-effort."""
    try:
        status = _read_status_file(cache_dir, model)
        if status is not None and status.get("state") != "downloading":
            return False
        if status is None:
            return True  # download not started yet; keep waiting
        progress = min(1.0, _model_cache_bytes(model, cache_dir) / expected_bytes) if expected_bytes > 0 else 0.0
        _write_status_file(cache_dir, state="downloading", model=model, error=None, progress=progress)
        return True
    except OSError:
        return True


def _downloading_stale(cache_dir: Path, model: str, status: dict[str, object]) -> bool:
    """True when a "downloading" status has not been refreshed within
    _DOWNLOAD_STALE_SECONDS — i.e. the downloading process died. Prefers the
    payload's updated_at; legacy status files carry no timestamp and fall
    back to the status file's mtime."""
    updated_at = status.get("updated_at")
    if isinstance(updated_at, str):
        with contextlib.suppress(ValueError):
            return time.time() - datetime.fromisoformat(updated_at).timestamp() > _DOWNLOAD_STALE_SECONDS
    mtimes = [
        path.stat().st_mtime
        for path in (_status_path(cache_dir, model), cache_dir / _STATUS_NAME)
        if path.is_file()
    ]
    return bool(mtimes) and time.time() - max(mtimes) > _DOWNLOAD_STALE_SECONDS


def local_model_status(model: str, cache_dir: str | Path) -> dict[str, object]:
    """Status payload for the settings endpoint; never raises.

    Status files are per model (status.{slug}.json); the legacy single
    status.json counts only when it names this model — files on disk for a
    different model read as not_downloaded.
    """
    info = LOCAL_EMBEDDING_MODELS.get(model, {"dimension": 0, "size_mb": 0})
    if info.get("backend") == "llama":
        # GGUF models live outside the fastembed cache layout entirely.
        from proseforge.infrastructure.embeddings.llama_server import llama_model_status

        return llama_model_status(model, cache_dir)
    payload: dict[str, object] = {
        "status": "not_downloaded",
        "error": None,
        "progress": None,
        "model": model,
        "size_mb": info["size_mb"],
        "dimension": info["dimension"],
    }
    cache_root = Path(cache_dir).expanduser()
    status = _read_status_file(cache_root, model)
    if status is None:
        # No status file for this model: a bundled/pre-fetched snapshot
        # (packaging/models, no status file) is still ready to use.
        if _model_snapshot_ready(model, cache_root):
            payload["status"] = "ready"
        return payload
    state = status.get("state")
    if state == "downloading" and _downloading_stale(cache_root, model, status):
        # The downloader crashed mid-download: surface a failure (persisted
        # best-effort so peer waiters also stop) instead of an eternal
        # "downloading" the frontend would poll forever.
        stalled_error = (
            "download stalled: no progress update for over 30 minutes "
            "(the downloading process likely crashed) — retry the download"
        )
        with contextlib.suppress(OSError):
            _write_status_file(cache_root, state="error", model=model, error=stalled_error)
        payload["status"] = "error"
        payload["error"] = stalled_error
        return payload
    if state in {"downloading", "ready"}:
        payload["status"] = state
        payload["progress"] = status.get("progress")
    elif state == "error":
        payload["status"] = "error"
        payload["error"] = status.get("error")
    return payload


@contextlib.contextmanager
def _hf_offline_lifted():
    """Temporarily lift HF offline mode so a NEW model can be downloaded.

    Loading a ready snapshot arms HF_HUB_OFFLINE=1 process-wide, and once
    huggingface_hub is imported its constants.HF_HUB_OFFLINE is baked in
    (huggingface_hub 1.25 reads it via constants.is_offline_mode() on every
    HTTP request). A later download of a DIFFERENT model in the same process
    would then die with OfflineModeIsEnabled — native single-process installs
    hit this. Lift the env var and every already-imported huggingface_hub /
    fastembed module-level binding for the download, restoring all of them
    afterwards (also on failure) so ready snapshots keep loading offline.
    """
    previous_env = os.environ.pop("HF_HUB_OFFLINE", None)
    patched: list[tuple[object, object]] = []
    for module in list(sys.modules.values()):
        name = getattr(module, "__name__", "") or ""
        if not (name.startswith(("huggingface_hub", "fastembed"))):
            continue
        if getattr(module, "HF_HUB_OFFLINE", None):
            patched.append((module, module.HF_HUB_OFFLINE))
            module.HF_HUB_OFFLINE = False
    try:
        yield
    finally:
        if previous_env is not None:
            os.environ["HF_HUB_OFFLINE"] = previous_env
        for module, previous in patched:
            module.HF_HUB_OFFLINE = previous


def _load_text_embedding(model: str, cache_dir: Path, threads: int, hf_endpoint: str | None):
    """Instantiate fastembed's TextEmbedding (downloads the model on first use).

    fastembed is an optional heavy dependency: imported lazily so installs
    without it can still run everything except the local engine.
    HF_ENDPOINT must be set before the import — huggingface_hub reads
    it once at import time.
    """
    if hf_endpoint:
        # HF_ENDPOINT is what huggingface_hub honors (1.24); HF_HUB_ENDPOINT
        # is kept for older versions and other hf tooling.
        os.environ.setdefault("HF_ENDPOINT", hf_endpoint)
        os.environ.setdefault("HF_HUB_ENDPOINT", hf_endpoint)
    # hf-xet fetches file content from cas-server.xethub.hf.co directly —
    # mirrors do not proxy it (401) and CN networks cannot reach it. Plain
    # HTTP CDN download through the endpoint works everywhere. (Verified on
    # huggingface_hub 1.24 + hf-xet 1.5.2.)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    snapshot_ready = _model_snapshot_ready(model, cache_dir)
    if snapshot_ready:
        # Bundled/previously downloaded snapshot present: load fully offline
        # (verified: HF_HUB_OFFLINE=1 + prefilled hub cache embeds fine).
        # Without a snapshot we stay online and pull through the mirror.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    try:
        from fastembed import TextEmbedding
    except ImportError as error:
        raise EmbeddingError(
            "local embedding engine requires fastembed; install it with: pip install fastembed"
        ) from error
    if snapshot_ready:
        return TextEmbedding(model_name=model, cache_dir=str(cache_dir), threads=threads)
    # No snapshot: this instantiation DOWNLOADS. A ready sibling model may
    # have armed offline mode earlier in this same process — lift it for the
    # download (restored afterwards by the context manager).
    with _hf_offline_lifted():
        return TextEmbedding(model_name=model, cache_dir=str(cache_dir), threads=threads)


def _ensure_model_ready(model: str, cache_dir: Path, threads: int, hf_endpoint: str | None):
    """Download (if needed) and return a ready TextEmbedding instance.

    Cross-process coordination via cache_dir/download.lock: the lock holder
    downloads and writes the per-model status file; concurrent callers poll
    it until ready/error or a 10-minute timeout. Raises EmbeddingError on
    failure so the indexing job goes through its normal retry chain.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    status = _read_status_file(cache_dir, model)
    if status is not None and status.get("state") == "ready":
        return _load_text_embedding(model, cache_dir, threads, hf_endpoint)

    lock_path = cache_dir / _LOCK_NAME
    deadline = time.monotonic() + _DOWNLOAD_TIMEOUT_SECONDS
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = time.time() - lock_path.stat().st_mtime
            if age > _LOCK_STALE_SECONDS:
                logger.warning("removing stale embedding download lock (age %.0fs)", age)
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            status = _read_status_file(cache_dir, model)
            if status is not None:
                if status.get("state") == "ready":
                    return _load_text_embedding(model, cache_dir, threads, hf_endpoint)
                if status.get("state") == "error":
                    raise EmbeddingError(f"local embedding model download failed: {status.get('error')}")
            if time.monotonic() > deadline:
                raise EmbeddingError(f"timed out waiting for local embedding model {model} download")
            time.sleep(_POLL_INTERVAL_SECONDS)
        else:
            os.close(fd)
            break

    # Lock held: this process downloads. The lock file's presence is the
    # mutex; the per-model status file tells waiters what is happening.
    try:
        _write_status_file(cache_dir, state="downloading", model=model, error=None)
        instance = _load_text_embedding(model, cache_dir, threads, hf_endpoint)
        _write_status_file(cache_dir, state="ready", model=model, error=None)
        return instance
    except EmbeddingError as error:
        _write_status_file(cache_dir, state="error", model=model, error=str(error)[:500])
        raise
    except Exception as error:
        _write_status_file(cache_dir, state="error", model=model, error=str(error)[:500])
        raise EmbeddingError(f"local embedding model download failed: {error}") from error
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


class LocalEmbedder:
    """Drop-in local replacement for EmbeddingClient (same ``async embed``)."""

    def __init__(
        self,
        model: str,
        *,
        cache_dir: str | Path,
        threads: int = 2,
        hf_endpoint: str | None = None,
    ):
        if model not in LOCAL_EMBEDDING_MODELS:
            raise ValueError(f"unsupported local embedding model: {model}")
        if LOCAL_EMBEDDING_MODELS[model].get("backend") == "llama":
            raise ValueError(f"{model} is served by llama.cpp; use LlamaServerEmbedder instead")
        self.model = model
        self.cache_dir = Path(cache_dir).expanduser()
        self.threads = threads
        self.hf_endpoint = hf_endpoint
        self._text_embedding = None  # loaded lazily by ensure_ready()

    @property
    def identity(self) -> str:
        """Identity string written to chunks and used for 409 conflict checks."""
        return f"local/{self.model}"

    @property
    def dimension(self) -> int:
        return int(LOCAL_EMBEDDING_MODELS[self.model]["dimension"])

    async def _track_download_progress(self) -> None:
        """Publish byte progress to the status file while a download runs;
        stops itself once the download leaves the "downloading" state."""
        expected_bytes = int(LOCAL_EMBEDDING_MODELS[self.model]["size_mb"]) * 1024 * 1024
        while await asyncio.to_thread(_write_download_progress, self.model, self.cache_dir, expected_bytes):
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def ensure_ready(self) -> None:
        """Download the model on first use; EmbeddingError on failure."""
        if self._text_embedding is not None:
            return
        sampler = asyncio.create_task(self._track_download_progress())
        try:
            self._text_embedding = await asyncio.to_thread(
                _ensure_model_ready, self.model, self.cache_dir, self.threads, self.hf_endpoint
            )
        finally:
            sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler

    def _truncate(self, texts: list[str]) -> tuple[list[str], list[int]]:
        truncated: list[int] = []
        clipped: list[str] = []
        for index, text in enumerate(texts):
            if len(text) > LOCAL_MAX_INPUT_CHARS:
                truncated.append(index)
                logger.warning(
                    "local embedding input %d truncated from %d to %d chars",
                    index, len(text), LOCAL_MAX_INPUT_CHARS,
                )
                clipped.append(text[:LOCAL_MAX_INPUT_CHARS])
            else:
                clipped.append(text)
        return clipped, truncated

    def _embed_sync(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        # See module docstring: fastembed does not add e5 prefixes itself.
        if "e5" in self.model.lower():
            prefix = "query: " if query else "passage: "
            texts = [f"{prefix}{text}" for text in texts]
        embeddings = list(self._text_embedding.embed(texts))
        if len(embeddings) != len(texts):
            raise EmbeddingError(f"local embedding count mismatch: sent {len(texts)}, got {len(embeddings)}")
        return [[float(value) for value in vector] for vector in embeddings]

    async def _embed_texts(self, texts: list[str], *, query: bool) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=[], total_tokens=0)
        await self.ensure_ready()
        clipped, truncated = self._truncate(texts)
        # ONNX inference is synchronous CPU work; keep it off the event loop.
        vectors = await asyncio.to_thread(self._embed_sync, clipped, query=query)
        total_tokens = sum(max(1, len(text) // 2) for text in clipped)
        return EmbeddingResult(vectors=vectors, total_tokens=total_tokens, truncated=truncated)

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed document texts (indexing side; same contract as EmbeddingClient.embed)."""
        return await self._embed_texts(texts, query=False)

    async def embed_query(self, texts: list[str]) -> EmbeddingResult:
        """Embed query texts (retrieval side): e5 models take the "query: " prefix."""
        return await self._embed_texts(texts, query=True)
