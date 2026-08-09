"""One-shot ops cleanup: delete trap-state retrieval_documents (0048 follow-up).

0048 cleared retrieval_chunks (force-rebuild) but left retrieval_documents
rows behind. The indexing worker's idempotency check (a document whose
source_version matches the job's source version settles as "skipped", zero
writes) then trapped every source whose document survived with NO chunks:
reindex jobs keep settling as skipped and the content never re-enters the
RAG index — the "skip trap".

The production server has already force-rebuilt its good indexes, so a
blanket DELETE FROM retrieval_documents is off the table (it would destroy
the rebuilt index). This migration deletes ONLY trap-state documents —
rows with no active chunk at all — and keeps every rebuilt document
intact. Re-indexing of the cleared rows is driven by the application's
normal enqueue flow (the next set_active_version / sweeper pass).

Idempotent by nature (the predicate is false once the trap rows are gone)
and inspector-guarded for databases where the retrieval tables do not
exist yet. Dialect-agnostic: plain conditional DELETE, no PG-only syntax.
"""

import sqlalchemy as sa
from alembic import op

revision = "0051_clear_retrieval_documents"
down_revision = "0050_model_catalog_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not {"retrieval_documents", "retrieval_chunks"} <= set(inspector.get_table_names()):
        return
    op.execute(sa.text(
        "DELETE FROM retrieval_documents WHERE NOT EXISTS ("
        "SELECT 1 FROM retrieval_chunks "
        "WHERE retrieval_chunks.document_id = retrieval_documents.id "
        "AND retrieval_chunks.status = 'active')"
    ))


def downgrade() -> None:
    # Irreversible by design: the deleted rows were trap-state garbage
    # (documents with zero active chunks), not recoverable index data.
    pass
