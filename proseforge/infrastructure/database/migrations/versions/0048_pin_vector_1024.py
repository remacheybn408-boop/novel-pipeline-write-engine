"""Pin retrieval_chunks.embedding to vector(1024) + HNSW index (bge-m3 convergence).

Third step of the locked four-step RAG convergence (hide other local models
-> force full rebuild -> pin the dimension -> restore). The convergence
target BAAI/bge-m3 embeds at 1024 dimensions, so the dimensionless ``vector``
column from 0037 is pinned to ``vector(1024)`` and gets an HNSW index
(cosine distance) to retire exact-scan latency.

Order inside this migration is deliberate and locked:

1. DELETE every retrieval_chunks row (force-rebuild semantics — mixed
   512/1024-dim leftovers would hard-fail the ALTER or, worse, poison the
   pinned column). Re-indexing is driven by the application's force flow.
2. ALTER the column to vector(1024). After this, inserting a wrong-dimension
   vector hard-fails at the database — that is the intended reconciliation
   sentinel against stale-engine writes.
3. CREATE the HNSW index (pgvector ``USING hnsw (embedding vector_cosine_ops)``).

PostgreSQL only: other dialects keep the JSON-text fallback column, so the
migration is a no-op there. Index creation is inspector-guarded/idempotent;
the DELETE and ALTER are not re-runnable by nature (empty table -> ALTER is
instant) and are protected by the same dialect guard.
"""

import sqlalchemy as sa
from alembic import op

revision = "0048_pin_vector_1024"
down_revision = "0047_recap_rollups"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1024
HNSW_INDEX_NAME = "ix_retrieval_chunks_embedding_hnsw"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite/dev: embedding stays JSON text; nothing to pin or index.
        return
    # 1) Force-rebuild semantics: clear all chunks BEFORE pinning. Rows are
    #    rebuilt by the application's force reindex flow.
    op.execute(sa.text("DELETE FROM retrieval_chunks"))
    # 2) Pin the dimension. With the table empty this is a metadata-only
    #    rewrite; wrong-dimension inserts hard-fail from now on (sentinel).
    op.execute(sa.text(f"ALTER TABLE retrieval_chunks ALTER COLUMN embedding TYPE vector({EMBEDDING_DIM})"))
    # 3) HNSW over cosine distance (pgvector opclass vector_cosine_ops).
    inspector = sa.inspect(bind)
    existing_indexes = {index["name"] for index in inspector.get_indexes("retrieval_chunks")}
    if HNSW_INDEX_NAME not in existing_indexes:
        op.execute(sa.text(
            f"CREATE INDEX {HNSW_INDEX_NAME} ON retrieval_chunks "
            "USING hnsw (embedding vector_cosine_ops)"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    existing_indexes = {index["name"] for index in inspector.get_indexes("retrieval_chunks")}
    if HNSW_INDEX_NAME in existing_indexes:
        op.execute(sa.text(f"DROP INDEX {HNSW_INDEX_NAME}"))
    # Back to the dimensionless vector (exact scan); rows written under the
    # pinned column are 1024-dim and remain valid under the wider type.
    op.execute(sa.text("ALTER TABLE retrieval_chunks ALTER COLUMN embedding TYPE vector"))
