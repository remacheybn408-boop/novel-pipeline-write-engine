"""Embedding client for narrative RAG (OpenAI-compatible /embeddings)."""

from proseforge.infrastructure.embeddings.client import (
    EmbeddingClient,
    EmbeddingError,
    EmbeddingResult,
    batch_limit_for,
)

__all__ = ["EmbeddingClient", "EmbeddingError", "EmbeddingResult", "batch_limit_for"]
