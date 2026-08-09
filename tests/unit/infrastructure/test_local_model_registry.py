"""Local embedding registry: visible/hidden filtering (bge-m3 convergence)
and per-model chunk_chars (按模型定窗). Hidden entries must stay fully
functional in the registry — rollback is a config change, not a code change.
"""

from __future__ import annotations

from proseforge.infrastructure.embeddings.local import (
    DEFAULT_LOCAL_CHUNK_CHARS,
    DEFAULT_LOCAL_MODEL,
    LOCAL_EMBEDDING_MODELS,
    local_model_chunk_chars,
    visible_local_models,
)


def test_only_bge_m3_is_visible():
    assert set(visible_local_models()) == {"BAAI/bge-m3"}


def test_default_model_is_the_visible_bge_m3():
    assert DEFAULT_LOCAL_MODEL == "BAAI/bge-m3"
    assert DEFAULT_LOCAL_MODEL in visible_local_models()
    assert int(LOCAL_EMBEDDING_MODELS[DEFAULT_LOCAL_MODEL]["dimension"]) == 1024


def test_hidden_models_stay_registered_for_rollback():
    """fetch.py references and the PUT whitelist read the FULL registry."""
    hidden = set(LOCAL_EMBEDDING_MODELS) - set(visible_local_models())
    assert hidden == {
        "BAAI/bge-small-zh-v1.5",
        "intfloat/multilingual-e5-large",
        "Qwen/Qwen3-Embedding-0.6B",
        "Qwen/Qwen3-Embedding-4B",
    }
    # Rollback metadata intact: hf_source/dimension/size untouched.
    assert LOCAL_EMBEDDING_MODELS["BAAI/bge-small-zh-v1.5"]["hf_source"] == "Qdrant/bge-small-zh-v1.5"
    assert int(LOCAL_EMBEDDING_MODELS["intfloat/multilingual-e5-large"]["dimension"]) == 1024


def test_chunk_chars_per_model():
    # bge-m3: ~1200-char window behind its 8k context.
    assert local_model_chunk_chars("BAAI/bge-m3") == 1200
    # 512-token fastembed models keep the historic 450-char window.
    assert local_model_chunk_chars("BAAI/bge-small-zh-v1.5") == 450
    assert local_model_chunk_chars("intfloat/multilingual-e5-large") == 450
    # Hidden llama.cpp models share the 8k server context.
    assert local_model_chunk_chars("Qwen/Qwen3-Embedding-0.6B") == 1200
    assert local_model_chunk_chars("Qwen/Qwen3-Embedding-4B") == 1200


def test_chunk_chars_fallback_for_unknown_model():
    assert local_model_chunk_chars("foo/bar") == DEFAULT_LOCAL_CHUNK_CHARS
