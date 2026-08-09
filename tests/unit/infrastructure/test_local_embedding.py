"""Local embedding engine (fastembed ONNX): whitelist, download lock/status
coordination, truncation, e5 passage prefix, bundled-snapshot offline
detection. fastembed is replaced in sys.modules with a fake — no model is
ever downloaded here.
"""

from __future__ import annotations

import json
import os
import sys
import time
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

from proseforge.infrastructure.embeddings import local
from proseforge.infrastructure.embeddings.client import EmbeddingError
from proseforge.infrastructure.embeddings.local import LocalEmbedder, local_model_status


class _FakeTextEmbedding:
    instances: ClassVar[list[_FakeTextEmbedding]] = []

    def __init__(self, model_name, cache_dir=None, threads=None):
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.threads = threads
        self.received: list[str] = []
        # Offline state visible at construction time — a real download through
        # huggingface_hub dies with OfflineModeIsEnabled when either is armed.
        self.offline_env_seen = os.environ.get("HF_HUB_OFFLINE")
        constants = sys.modules.get("huggingface_hub.constants")
        self.offline_constant_seen = getattr(constants, "HF_HUB_OFFLINE", None) if constants is not None else None
        type(self).instances.append(self)

    def embed(self, texts, **_kwargs):
        self.received.extend(texts)
        for index, _text in enumerate(texts):
            yield [float(index), 0.5]


@pytest.fixture()
def fake_fastembed(monkeypatch):
    _FakeTextEmbedding.instances = []
    module = types.ModuleType("fastembed")
    module.TextEmbedding = _FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", module)
    return module


def test_unsupported_model_rejected(tmp_path):
    with pytest.raises(ValueError, match="unsupported local embedding model"):
        LocalEmbedder("foo/bar", cache_dir=tmp_path)


@pytest.mark.asyncio
async def test_embed_marks_ready_and_returns_vectors(fake_fastembed, tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HUB_ENDPOINT", raising=False)
    embedder = LocalEmbedder(
        "BAAI/bge-small-zh-v1.5", cache_dir=tmp_path, threads=3, hf_endpoint="https://hf.example.com"
    )
    assert embedder.identity == "local/BAAI/bge-small-zh-v1.5"
    assert embedder.dimension == 512

    result = await embedder.embed(["第一段", "第二段"])

    assert result.vectors == [[0.0, 0.5], [1.0, 0.5]]
    assert result.truncated == []
    instance = _FakeTextEmbedding.instances[0]
    assert instance.model_name == "BAAI/bge-small-zh-v1.5"
    assert instance.cache_dir == str(tmp_path)
    assert instance.threads == 3
    assert os.environ["HF_HUB_ENDPOINT"] == "https://hf.example.com"
    status = local_model_status("BAAI/bge-small-zh-v1.5", tmp_path)
    assert status["status"] == "ready" and status["error"] is None


@pytest.mark.asyncio
async def test_embed_truncates_overlong_input(fake_fastembed, tmp_path):
    embedder = LocalEmbedder("BAAI/bge-small-zh-v1.5", cache_dir=tmp_path)

    result = await embedder.embed(["字" * 1000])

    assert result.truncated == [0]
    assert len(_FakeTextEmbedding.instances[0].received[0]) == local.LOCAL_MAX_INPUT_CHARS


@pytest.mark.asyncio
async def test_e5_inputs_get_passage_prefix(fake_fastembed, tmp_path):
    embedder = LocalEmbedder("intfloat/multilingual-e5-large", cache_dir=tmp_path)

    await embedder.embed(["正文"])

    assert _FakeTextEmbedding.instances[0].received == ["passage: 正文"]


@pytest.mark.asyncio
async def test_e5_queries_get_query_prefix(fake_fastembed, tmp_path):
    """Regression (M12): retrieval queries must take the "query: " prefix —
    embedding them with the passage prefix silently degrades e5 retrieval."""
    embedder = LocalEmbedder("intfloat/multilingual-e5-large", cache_dir=tmp_path)

    result = await embedder.embed_query(["主角是谁"])

    assert result.vectors == [[0.0, 0.5]]
    assert _FakeTextEmbedding.instances[0].received == ["query: 主角是谁"]


@pytest.mark.asyncio
async def test_non_e5_query_gets_no_prefix(fake_fastembed, tmp_path):
    embedder = LocalEmbedder("BAAI/bge-small-zh-v1.5", cache_dir=tmp_path)

    await embedder.embed_query(["主角是谁"])

    assert _FakeTextEmbedding.instances[0].received == ["主角是谁"]


@pytest.mark.asyncio
async def test_empty_input_short_circuits(fake_fastembed, tmp_path):
    embedder = LocalEmbedder("BAAI/bge-small-zh-v1.5", cache_dir=tmp_path)

    result = await embedder.embed([])

    assert result.vectors == [] and _FakeTextEmbedding.instances == []


@pytest.mark.asyncio
async def test_missing_fastembed_has_clear_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)  # import of fastembed fails
    embedder = LocalEmbedder("BAAI/bge-small-zh-v1.5", cache_dir=tmp_path)

    with pytest.raises(EmbeddingError, match="pip install fastembed"):
        await embedder.ensure_ready()

    assert local_model_status("BAAI/bge-small-zh-v1.5", tmp_path)["status"] == "error"


def test_status_for_other_model_is_not_downloaded(tmp_path):
    local._write_status_file(tmp_path, state="ready", model="BAAI/bge-small-zh-v1.5", error=None)

    status = local_model_status("intfloat/multilingual-e5-large", tmp_path)

    assert status["status"] == "not_downloaded"
    assert status["dimension"] == 1024
    assert status["size_mb"] == 2100


def test_per_model_status_files_are_independent(tmp_path):
    local._write_status_file(tmp_path, state="ready", model="BAAI/bge-small-zh-v1.5", error=None)
    local._write_status_file(tmp_path, state="error", model="intfloat/multilingual-e5-large", error="boom")

    assert local_model_status("BAAI/bge-small-zh-v1.5", tmp_path)["status"] == "ready"
    e5_status = local_model_status("intfloat/multilingual-e5-large", tmp_path)
    assert e5_status["status"] == "error" and e5_status["error"] == "boom"


def test_legacy_single_status_file_is_honored(tmp_path):
    """Installs predating the per-model split only have status.json; it counts
    when it names the requested model and is ignored otherwise."""
    (tmp_path / "status.json").write_text(
        json.dumps({"state": "ready", "model": "BAAI/bge-small-zh-v1.5", "error": None}), encoding="utf-8"
    )

    assert local_model_status("BAAI/bge-small-zh-v1.5", tmp_path)["status"] == "ready"
    assert local_model_status("intfloat/multilingual-e5-large", tmp_path)["status"] == "not_downloaded"


def test_progress_written_and_passed_through(tmp_path):
    local._write_status_file(tmp_path, state="downloading", model="BAAI/bge-small-zh-v1.5", error=None, progress=0.42)

    status = local_model_status("BAAI/bge-small-zh-v1.5", tmp_path)

    assert status["status"] == "downloading"
    assert status["progress"] == 0.42


def test_progress_is_none_without_download(tmp_path):
    assert local_model_status("BAAI/bge-small-zh-v1.5", tmp_path)["progress"] is None


def _age_status_file(path: Path, seconds: float) -> None:
    """Backdate a status file's updated_at payload and mtime."""
    stale_time = time.time() - seconds
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "updated_at" in payload:
        payload["updated_at"] = datetime.fromtimestamp(stale_time, UTC).isoformat()
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.utime(path, (stale_time, stale_time))


def test_stale_downloading_status_reported_as_failed(tmp_path):
    """Regression (L12a): a "downloading" status whose downloader crashed
    (no refresh beyond the stale threshold) must surface as failed instead
    of leaving the frontend polling forever."""
    local._write_status_file(tmp_path, state="downloading", model="BAAI/bge-small-zh-v1.5", error=None, progress=0.3)
    _age_status_file(local._status_path(tmp_path, "BAAI/bge-small-zh-v1.5"), local._DOWNLOAD_STALE_SECONDS + 60)

    status = local_model_status("BAAI/bge-small-zh-v1.5", tmp_path)

    assert status["status"] == "error"
    assert "stalled" in str(status["error"])
    # Persisted: the next read (and peer waiters) see the failure too.
    persisted = local._read_status_file(tmp_path, "BAAI/bge-small-zh-v1.5")
    assert persisted is not None and persisted["state"] == "error"


def test_fresh_downloading_status_not_marked_stale(tmp_path):
    """A live download refreshes the status file every few seconds — it must
    keep reporting "downloading", never a false stale failure."""
    local._write_status_file(tmp_path, state="downloading", model="BAAI/bge-small-zh-v1.5", error=None, progress=0.3)

    status = local_model_status("BAAI/bge-small-zh-v1.5", tmp_path)

    assert status["status"] == "downloading" and status["progress"] == 0.3


def test_legacy_downloading_status_staleness_uses_mtime(tmp_path):
    """Legacy status.json carries no updated_at: staleness falls back to the
    file mtime."""
    legacy = tmp_path / "status.json"
    legacy.write_text(
        json.dumps({"state": "downloading", "model": "BAAI/bge-small-zh-v1.5", "error": None}), encoding="utf-8"
    )
    _age_status_file(legacy, local._DOWNLOAD_STALE_SECONDS + 60)

    status = local_model_status("BAAI/bge-small-zh-v1.5", tmp_path)

    assert status["status"] == "error"


def test_write_download_progress_reports_cache_bytes(tmp_path):
    local._write_status_file(tmp_path, state="downloading", model="BAAI/bge-small-zh-v1.5", error=None)
    cache_root = tmp_path / "models--Qdrant--bge-small-zh-v1.5" / "snapshots" / "rev1"
    cache_root.mkdir(parents=True)
    (cache_root / "model.onnx").write_bytes(b"x" * 50)

    keep_going = local._write_download_progress("BAAI/bge-small-zh-v1.5", tmp_path, expected_bytes=100)

    assert keep_going is True
    status = local_model_status("BAAI/bge-small-zh-v1.5", tmp_path)
    assert status["status"] == "downloading" and status["progress"] == 0.5


def test_write_download_progress_stops_after_terminal_state(tmp_path):
    local._write_status_file(tmp_path, state="ready", model="BAAI/bge-small-zh-v1.5", error=None)

    keep_going = local._write_download_progress("BAAI/bge-small-zh-v1.5", tmp_path, expected_bytes=100)

    assert keep_going is False
    # The terminal status file must not be clobbered by the sampler.
    assert local_model_status("BAAI/bge-small-zh-v1.5", tmp_path)["status"] == "ready"


def test_write_download_progress_waits_before_download_starts(tmp_path):
    keep_going = local._write_download_progress("BAAI/bge-small-zh-v1.5", tmp_path, expected_bytes=100)

    assert keep_going is True
    # No status file existed: the sampler must not invent one.
    assert local_model_status("BAAI/bge-small-zh-v1.5", tmp_path)["status"] == "not_downloaded"


@pytest.mark.asyncio
async def test_peer_download_error_raises(fake_fastembed, tmp_path):
    (tmp_path / "download.lock").write_text("pid")
    local._write_status_file(tmp_path, state="error", model="BAAI/bge-small-zh-v1.5", error="boom")
    embedder = LocalEmbedder("BAAI/bge-small-zh-v1.5", cache_dir=tmp_path)

    with pytest.raises(EmbeddingError, match="boom"):
        await embedder.ensure_ready()


@pytest.mark.asyncio
async def test_wait_for_peer_download_times_out(fake_fastembed, tmp_path, monkeypatch):
    (tmp_path / "download.lock").write_text("pid")
    monkeypatch.setattr(local, "_DOWNLOAD_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(local, "_POLL_INTERVAL_SECONDS", 0.01)
    embedder = LocalEmbedder("BAAI/bge-small-zh-v1.5", cache_dir=tmp_path)

    with pytest.raises(EmbeddingError, match="timed out"):
        await embedder.ensure_ready()


def _make_snapshot(cache_dir: Path, source_repo: str = "Qdrant/bge-small-zh-v1.5") -> Path:
    """Fake a bundled/pre-downloaded hub snapshot (no status.json). The cache
    directory is named after fastembed's actual source repo (Qdrant's
    repackaged export), NOT the vendor repo id."""
    root = Path(cache_dir) / f"models--{source_repo.replace('/', '--')}" / "snapshots" / "rev1"
    root.mkdir(parents=True)
    (root / "model.onnx").write_bytes(b"fake-onnx")
    return root


def test_offline_env_set_when_snapshot_present(fake_fastembed, tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    _make_snapshot(tmp_path)

    local._load_text_embedding("BAAI/bge-small-zh-v1.5", tmp_path, 1, None)

    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_offline_env_not_set_without_snapshot(fake_fastembed, tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)

    local._load_text_embedding("BAAI/bge-small-zh-v1.5", tmp_path, 1, None)

    assert "HF_HUB_OFFLINE" not in os.environ


@pytest.fixture()
def fake_hf_constants(monkeypatch):
    """Fake huggingface_hub's import-time offline constant: once HF_HUB_OFFLINE=1
    is in the environment at import, constants.HF_HUB_OFFLINE stays True for the
    process lifetime (huggingface_hub 1.25: is_offline_mode() reads it on every
    HTTP request and raises OfflineModeIsEnabled)."""
    module = types.ModuleType("huggingface_hub.constants")
    module.HF_HUB_OFFLINE = False
    monkeypatch.setitem(sys.modules, "huggingface_hub.constants", module)
    return module


def test_second_model_download_lifts_offline_mode(fake_fastembed, fake_hf_constants, tmp_path, monkeypatch):
    """Regression: model A ready -> offline armed process-wide (env + the
    import-time huggingface_hub constant); downloading model B in the SAME
    process must lift offline mode for the download instead of dying with
    OfflineModeIsEnabled, and restore it afterwards."""
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    _make_snapshot(tmp_path)  # model A bundled/ready
    local._load_text_embedding("BAAI/bge-small-zh-v1.5", tmp_path, 1, None)
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    fake_hf_constants.HF_HUB_OFFLINE = True  # huggingface_hub imported while offline

    local._load_text_embedding("intfloat/multilingual-e5-large", tmp_path, 1, None)

    download = _FakeTextEmbedding.instances[-1]
    assert download.model_name == "intfloat/multilingual-e5-large"
    assert download.offline_env_seen is None  # env var lifted during the download
    assert download.offline_constant_seen is False  # import-time constant lifted too
    # Restored afterwards: ready snapshots keep loading fully offline.
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert fake_hf_constants.HF_HUB_OFFLINE is True


def test_offline_mode_restored_when_download_fails(fake_fastembed, fake_hf_constants, tmp_path, monkeypatch):
    """A failed download must still restore the offline env var and constant."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    fake_hf_constants.HF_HUB_OFFLINE = True

    class _FailingTextEmbedding:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("boom")

    fake_fastembed.TextEmbedding = _FailingTextEmbedding

    with pytest.raises(RuntimeError, match="boom"):
        local._load_text_embedding("intfloat/multilingual-e5-large", tmp_path, 1, None)

    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert fake_hf_constants.HF_HUB_OFFLINE is True


def test_status_ready_for_bundled_snapshot_without_status_file(tmp_path):
    _make_snapshot(tmp_path)

    status = local_model_status("BAAI/bge-small-zh-v1.5", tmp_path)

    assert status["status"] == "ready"
    assert status["error"] is None


def test_status_not_downloaded_without_snapshot_or_status_file(tmp_path):
    assert local_model_status("BAAI/bge-small-zh-v1.5", tmp_path)["status"] == "not_downloaded"


def test_vendor_repo_cache_dir_is_not_detected(tmp_path, monkeypatch):
    """Regression: a snapshot cached under the VENDOR repo id
    (models--BAAI--*) is invisible to fastembed and must not count."""
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    _make_snapshot(tmp_path, source_repo="BAAI/bge-small-zh-v1.5")

    assert local_model_status("BAAI/bge-small-zh-v1.5", tmp_path)["status"] == "not_downloaded"


def test_whitelist_maps_models_to_qdrant_source_repos():
    assert local.LOCAL_EMBEDDING_MODELS["BAAI/bge-small-zh-v1.5"]["hf_source"] == "Qdrant/bge-small-zh-v1.5"
    assert local.LOCAL_EMBEDDING_MODELS["intfloat/multilingual-e5-large"]["hf_source"] == "qdrant/multilingual-e5-large-onnx"
