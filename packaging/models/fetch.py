#!/usr/bin/env python3
"""Pre-fetch the whitelisted local embedding models into this directory.

Run once (online) before building an offline distribution:

    python packaging/models/fetch.py                  # both ONNX models
    python packaging/models/fetch.py --only-default   # bge-small-zh only (~90MB)
    python packaging/models/fetch.py --include-e5     # both, explicitly
    python packaging/models/fetch.py --include-gguf   # all three llama.cpp GGUF models
    python packaging/models/fetch.py --gguf BAAI/bge-m3   # one GGUF model

The download uses the standard HF hub cache layout, identical to what
fastembed creates at runtime, so the application detects the snapshots and
loads them with HF_HUB_OFFLINE=1 (see infrastructure/embeddings/local.py).
GGUF models land flat in ./gguf/<file>, matching the convention
infrastructure/embeddings/llama_server.py discovers at runtime.
HF_ENDPOINT defaults to the hf-mirror.com mirror and hf-xet is disabled
(mirrors do not proxy the xet CAS server) — both overridable via env.
Idempotent: models already on disk are skipped.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Must be set before huggingface_hub is imported (it reads env at import).
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

CACHE_DIR = Path(__file__).resolve().parent

# fastembed does NOT download the original vendor repos — it pulls Qdrant's
# repackaged ONNX exports (verified via TextEmbedding.list_supported_models()):
#   BAAI/bge-small-zh-v1.5        -> Qdrant/bge-small-zh-v1.5
#                                    (model_optimized.onnx + tokenizer, ~90MB)
#   intfloat/multilingual-e5-large -> qdrant/multilingual-e5-large-onnx
#                                    (model.onnx + model.onnx_data, ~2.1GB)
# Fetching the vendor repos would land in cache directories fastembed never
# looks at. Keep in sync with LOCAL_EMBEDDING_MODELS["..."]["hf_source"] in
# proseforge/infrastructure/embeddings/local.py.
DEFAULT_MODEL = "Qdrant/bge-small-zh-v1.5"
E5_MODEL = "qdrant/multilingual-e5-large-onnx"
MODELS: dict[str, int] = {DEFAULT_MODEL: 90, E5_MODEL: 2100}  # repo_id -> ~MB

# Only the runtime files — never whole-repo snapshots (an unfiltered pull of
# the e5 repo drags in ~6GB of PyTorch/flax weights fastembed cannot use).
ALLOW_PATTERNS = ["*.onnx", "*.onnx_data", "*.json", "*.txt", "*.model"]

# llama.cpp GGUF models: model id -> (gguf repo, gguf file, ~MB). Downloaded
# flat into ./gguf/<file>, the exact convention
# proseforge/infrastructure/embeddings/llama_server.py (LLAMA_MODELS)
# discovers at runtime — keep the two tables in sync.
GGUF_DIR = CACHE_DIR / "gguf"
GGUF_MODELS: dict[str, tuple[str, str, int]] = {
    "BAAI/bge-m3": ("gpustack/bge-m3-GGUF", "bge-m3-Q8_0.gguf", 700),
    "Qwen/Qwen3-Embedding-0.6B": ("Qwen/Qwen3-Embedding-0.6B-GGUF", "Qwen3-Embedding-0.6B-Q8_0.gguf", 640),
    "Qwen/Qwen3-Embedding-4B": ("Qwen/Qwen3-Embedding-4B-GGUF", "Qwen3-Embedding-4B-Q4_K_M.gguf", 2500),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only-default", action="store_true", help=f"fetch only {DEFAULT_MODEL} (~90MB)")
    parser.add_argument("--include-e5", action="store_true", help=f"also fetch {E5_MODEL} (~2.2GB)")
    parser.add_argument("--include-gguf", action="store_true", help="fetch all three llama.cpp GGUF models")
    parser.add_argument(
        "--gguf",
        action="append",
        choices=sorted(GGUF_MODELS),
        metavar="MODEL",
        help="fetch one GGUF model (repeatable), e.g. --gguf BAAI/bge-m3",
    )
    args = parser.parse_args(argv)
    if args.only_default and args.include_e5:
        parser.error("--only-default and --include-e5 are mutually exclusive")
    if (args.include_gguf or args.gguf) and (args.only_default or args.include_e5):
        parser.error("GGUF options cannot be combined with --only-default/--include-e5")
    if args.include_gguf and args.gguf:
        parser.error("--include-gguf and --gguf are mutually exclusive")
    return args


def selected_models(args: argparse.Namespace) -> list[str]:
    if args.only_default:
        return [DEFAULT_MODEL]
    if args.include_gguf or args.gguf:
        return []
    return list(MODELS)


def selected_gguf_models(args: argparse.Namespace) -> list[str]:
    if args.include_gguf:
        return list(GGUF_MODELS)
    return list(args.gguf or [])


def snapshot_ready(model: str, cache_dir: Path = CACHE_DIR) -> bool:
    """True when the hub cache already holds ONNX files for this model."""
    snapshot_root = cache_dir / f"models--{model.replace('/', '--')}" / "snapshots"
    return any(snapshot_root.glob("*/**/*.onnx"))


def dir_size_bytes(path: Path) -> int:
    # HF caches hardlink snapshot files to blobs: count each inode once so
    # the reported footprint matches du, not double the real size.
    seen: set[tuple[int, int]] = set()
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            stat = item.stat()
            key = (stat.st_dev, stat.st_ino)
            if key not in seen:
                seen.add(key)
                total += stat.st_size
    return total


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("huggingface_hub is required: pip install huggingface_hub", file=sys.stderr)
        return 1

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for model in selected_models(args):
        if snapshot_ready(model, CACHE_DIR):
            size_mb = dir_size_bytes(CACHE_DIR / f"models--{model.replace('/', '--')}") / 1e6
            print(f"[skip] {model}: snapshot already present ({size_mb:.0f} MB on disk)")
            continue
        print(f"[fetch] {model} (~{MODELS[model]} MB) via {os.environ['HF_ENDPOINT']} ...")
        snapshot_download(repo_id=model, cache_dir=str(CACHE_DIR), allow_patterns=ALLOW_PATTERNS)
        size_mb = dir_size_bytes(CACHE_DIR / f"models--{model.replace('/', '--')}") / 1e6
        print(f"[done] {model}: {size_mb:.0f} MB on disk")
    for model in selected_gguf_models(args):
        from huggingface_hub import (
            hf_hub_download,  # lazy: ONNX-only runs never need it
        )

        repo_id, filename, expected_mb = GGUF_MODELS[model]
        target = GGUF_DIR / filename
        if target.is_file() and target.stat().st_size > 0:
            print(f"[skip] {model}: {target} already present ({target.stat().st_size / 1e6:.0f} MB)")
            continue
        print(f"[fetch] {model}: {repo_id}/{filename} (~{expected_mb} MB) via {os.environ['HF_ENDPOINT']} ...")
        GGUF_DIR.mkdir(parents=True, exist_ok=True)
        hf_hub_download(repo_id=repo_id, filename=filename, local_dir=str(GGUF_DIR))
        print(f"[done] {model}: {target.stat().st_size / 1e6:.0f} MB on disk")
    print(f"Models cached in {CACHE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
