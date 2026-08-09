"""Narrative RAG retrieval tables + user_preferences; drop legacy embeddings.

Creates the phase-1 retrieval schema (documents/chunks/jobs/runs/
canon_conflicts, all project-owned with ON DELETE CASCADE per the 0035
convention) plus the user_preferences key/value table. The chunk embedding
column is a dimensionless pgvector ``vector`` on PostgreSQL (exact scan
only — pin vector(n) and add HNSW once the embedding model is fixed per
installation) and JSON text elsewhere. The legacy ``embeddings`` table
(superseded by retrieval_chunks) is dropped. Inspector-guarded and
idempotent; downgrade recreates the legacy table shell and drops the new
tables.
"""

import sqlalchemy as sa
from alembic import op

revision = "0037_narrative_rag_retrieval"
down_revision = "0036_model_modality_cleanup"
branch_labels = None
depends_on = None


def _embedding_column() -> sa.Column:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        from pgvector.sqlalchemy import Vector

        return sa.Column("embedding", Vector(), nullable=True)
    return sa.Column("embedding", sa.Text(), nullable=True)


def _project_fk(column: str = "project_id") -> sa.Column:
    return sa.Column(
        column, sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "user_preferences" not in existing:
        op.create_table(
            "user_preferences",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False, index=True),
            sa.Column("key", sa.String(100), nullable=False),
            sa.Column("value_json", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "key", name="uq_user_preferences_user_key"),
        )

    if "retrieval_documents" not in existing:
        op.create_table(
            "retrieval_documents",
            sa.Column("id", sa.String(64), primary_key=True),
            _project_fk(),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_id", sa.String(64), nullable=False),
            sa.Column("source_version", sa.String(64), nullable=False),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("authority_level", sa.String(32), nullable=False),
            sa.Column("chapter_from", sa.Integer(), nullable=True),
            sa.Column("chapter_to", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )

    if "retrieval_chunks" not in existing:
        op.create_table(
            "retrieval_chunks",
            sa.Column("id", sa.String(64), primary_key=True),
            _project_fk(),
            sa.Column(
                "document_id", sa.String(64),
                sa.ForeignKey("retrieval_documents.id", ondelete="CASCADE"), nullable=False, index=True,
            ),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("metadata_json", sa.Text(), nullable=False),
            sa.Column("search_text", sa.Text(), nullable=False),
            _embedding_column(),
            sa.Column("embedding_model", sa.String(200), nullable=False),
            sa.Column("embedding_version", sa.String(64), nullable=False),
            sa.Column("token_count", sa.Integer(), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "retrieval_jobs" not in existing:
        op.create_table(
            "retrieval_jobs",
            sa.Column("id", sa.String(64), primary_key=True),
            _project_fk(),
            sa.Column("job_type", sa.String(32), nullable=False),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_id", sa.String(64), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )

    if "retrieval_runs" not in existing:
        op.create_table(
            "retrieval_runs",
            sa.Column("id", sa.String(64), primary_key=True),
            _project_fk(),
            sa.Column("conversation_id", sa.String(64), nullable=True),
            sa.Column("message_id", sa.String(64), nullable=True),
            sa.Column("query_text", sa.Text(), nullable=False),
            sa.Column("intent", sa.String(64), nullable=False),
            sa.Column("filters_json", sa.Text(), nullable=False),
            sa.Column("selected_chunks_json", sa.Text(), nullable=False),
            sa.Column("elapsed_ms", sa.Float(), nullable=False),
            sa.Column("token_cost", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "canon_conflicts" not in existing:
        op.create_table(
            "canon_conflicts",
            sa.Column("id", sa.String(64), primary_key=True),
            _project_fk(),
            sa.Column("candidate_source", sa.String(128), nullable=False),
            sa.Column("conflicting_source", sa.String(128), nullable=False),
            sa.Column("field_or_claim", sa.Text(), nullable=False),
            sa.Column("evidence_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("resolved_by", sa.String(64), nullable=True),
        )

    if "embeddings" in existing:
        op.drop_table("embeddings")


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    # Recreate the legacy embeddings shell (columns as of 0035); its rows
    # are not recoverable — re-indexing rebuilds equivalent data in
    # retrieval_chunks.
    if "embeddings" not in existing:
        op.create_table(
            "embeddings",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column(
                "project_id", sa.String(64),
                sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True,
            ),
            sa.Column("source_type", sa.String(64), nullable=False),
            sa.Column("source_id", sa.String(64), nullable=False),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("embedding_model", sa.String(200), nullable=False),
            sa.Column("vector_json", sa.Text(), nullable=False),
            sa.UniqueConstraint(
                "project_id", "source_type", "source_id", "chunk_index", "embedding_model",
                name="uq_embeddings_source",
            ),
        )

    for table in (
        "canon_conflicts", "retrieval_runs", "retrieval_jobs",
        "retrieval_chunks", "retrieval_documents", "user_preferences",
    ):
        if table in existing:
            op.drop_table(table)
