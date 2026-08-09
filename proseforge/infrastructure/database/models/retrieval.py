"""Narrative RAG retrieval tables (migration 0037).

The embedding column is declared as an undimensioned ``vector`` at the ORM
level (JSON text fallback elsewhere); migration 0048 pins the PostgreSQL
column to ``vector(1024)`` — the bge-m3 convergence dimension — and adds an
HNSW index. The ORM stays dimensionless on purpose: pgvector binds vectors
as literals either way, so hidden-registry rollback models keep loading,
and wrong-dimension writes hard-fail at the database as the reconciliation
sentinel. The application-level guard lives in indexing.py (registry
dimension vs actual vector width).
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from proseforge.infrastructure.database.base import Base


class VectorOrJson(TypeDecorator):
    """pgvector ``vector`` (dimensionless) on PostgreSQL, JSON text elsewhere."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value
        return json.loads(value)


class RetrievalDocumentModel(Base):
    __tablename__ = "retrieval_documents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    authority_level: Mapped[str] = mapped_column(String(32), nullable=False, default="canon")
    chapter_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chapter_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RetrievalChunkModel(Base):
    __tablename__ = "retrieval_chunks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(64), ForeignKey("retrieval_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    embedding: Mapped[list[float] | None] = mapped_column(VectorOrJson(), nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    embedding_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RetrievalJobModel(Base):
    __tablename__ = "retrieval_jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RetrievalRunModel(Base):
    __tablename__ = "retrieval_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    query_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    intent: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    filters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    selected_chunks_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    elapsed_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    token_cost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CanonConflictModel(Base):
    __tablename__ = "canon_conflicts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    candidate_source: Mapped[str] = mapped_column(String(128), nullable=False)
    conflicting_source: Mapped[str] = mapped_column(String(128), nullable=False)
    field_or_claim: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
