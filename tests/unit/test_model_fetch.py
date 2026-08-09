"""packaging/models/fetch.py: model selection, idempotency, mirror env
defaults. huggingface_hub is faked via sys.modules — nothing downloads.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path

import pytest

from packaging.models import fetch


def _fake_hub(monkeypatch, calls: list[str]) -> None:
    module = types.ModuleType("huggingface_hub")

    def snapshot_download(repo_id, cache_dir, allow_patterns=None):
        calls.append(repo_id)
        assert allow_patterns == fetch.ALLOW_PATTERNS
        root = Path(cache_dir) / f"models--{repo_id.replace('/', '--')}" / "snapshots" / "rev1"
        root.mkdir(parents=True, exist_ok=True)
        (root / "model.onnx").write_bytes(b"fake-onnx")

    module.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)


def _make_snapshot(cache_dir: Path, model: str) -> None:
    root = cache_dir / f"models--{model.replace('/', '--')}" / "snapshots" / "rev1"
    root.mkdir(parents=True)
    (root / "model.onnx").write_bytes(b"fake-onnx")


def test_default_fetches_both_models(tmp_path, monkeypatch, capsys):
    calls: list[str] = []
    _fake_hub(monkeypatch, calls)
    monkeypatch.setattr(fetch, "CACHE_DIR", tmp_path)

    assert fetch.main([]) == 0

    assert calls == [fetch.DEFAULT_MODEL, fetch.E5_MODEL]
    assert "MB on disk" in capsys.readouterr().out


def test_only_default_fetches_bge_only(tmp_path, monkeypatch):
    calls: list[str] = []
    _fake_hub(monkeypatch, calls)
    monkeypatch.setattr(fetch, "CACHE_DIR", tmp_path)

    fetch.main(["--only-default"])

    assert calls == [fetch.DEFAULT_MODEL]


def test_include_e5_fetches_both(tmp_path, monkeypatch):
    calls: list[str] = []
    _fake_hub(monkeypatch, calls)
    monkeypatch.setattr(fetch, "CACHE_DIR", tmp_path)

    fetch.main(["--include-e5"])

    assert calls == [fetch.DEFAULT_MODEL, fetch.E5_MODEL]


def test_only_default_and_include_e5_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        fetch.parse_args(["--only-default", "--include-e5"])


def test_existing_snapshot_is_skipped(tmp_path, monkeypatch, capsys):
    calls: list[str] = []
    _fake_hub(monkeypatch, calls)
    _make_snapshot(tmp_path, fetch.DEFAULT_MODEL)
    monkeypatch.setattr(fetch, "CACHE_DIR", tmp_path)

    fetch.main(["--only-default"])

    assert calls == []
    assert "[skip]" in capsys.readouterr().out


def test_mirror_env_defaults(monkeypatch):
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)

    importlib.reload(fetch)  # module-level setdefault runs on (re)import

    assert os.environ["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert os.environ["HF_HUB_DISABLE_XET"] == "1"


def test_fetch_sources_are_qdrant_repackaged_repos():
    """fastembed pulls Qdrant's ONNX repacks, never the vendor repos."""
    assert fetch.DEFAULT_MODEL == "Qdrant/bge-small-zh-v1.5"
    assert fetch.E5_MODEL == "qdrant/multilingual-e5-large-onnx"


def _fake_gguf_hub(monkeypatch, calls: list[tuple[str, str]]) -> None:
    module = types.ModuleType("huggingface_hub")
    module.snapshot_download = lambda *args, **kwargs: None  # ONNX path unused here

    def hf_hub_download(repo_id, filename, local_dir):
        calls.append((repo_id, filename))
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        (Path(local_dir) / filename).write_bytes(b"fake-gguf")

    module.hf_hub_download = hf_hub_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)


def test_include_gguf_fetches_all_three(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []
    _fake_gguf_hub(monkeypatch, calls)
    monkeypatch.setattr(fetch, "GGUF_DIR", tmp_path / "gguf")

    assert fetch.main(["--include-gguf"]) == 0

    assert calls == [(repo, filename) for repo, filename, _mb in fetch.GGUF_MODELS.values()]
    assert (tmp_path / "gguf" / "bge-m3-Q8_0.gguf").is_file()


def test_single_gguf_selection_and_idempotent_skip(tmp_path, monkeypatch, capsys):
    calls: list[tuple[str, str]] = []
    _fake_gguf_hub(monkeypatch, calls)
    monkeypatch.setattr(fetch, "GGUF_DIR", tmp_path / "gguf")

    fetch.main(["--gguf", "BAAI/bge-m3"])
    fetch.main(["--gguf", "BAAI/bge-m3"])  # already on disk: skipped

    assert calls == [("gpustack/bge-m3-GGUF", "bge-m3-Q8_0.gguf")]
    assert "[skip]" in capsys.readouterr().out


def test_gguf_options_reject_onnx_combinations():
    with pytest.raises(SystemExit):
        fetch.parse_args(["--include-gguf", "--only-default"])
    with pytest.raises(SystemExit):
        fetch.parse_args(["--include-gguf", "--gguf", "BAAI/bge-m3"])
