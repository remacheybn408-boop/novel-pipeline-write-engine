"""pg_trgm extension + GIN trigram index on retrieval_chunks.content.

Powers the keyword leg of narrative retrieval (task: hybrid search).
PostgreSQL only — sqlite skips both statements (the keyword leg falls
back to Python-side substring scoring there). Idempotent by nature.
"""

import sqlalchemy as sa
from alembic import op

revision = "0040_retrieval_trgm"
down_revision = "0039_characters_and_version_summary"
branch_labels = None
depends_on = None

_INDEX = "ix_retrieval_chunks_content_trgm"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    op.execute(sa.text(
        f"CREATE INDEX IF NOT EXISTS {_INDEX} ON retrieval_chunks USING gin (content gin_trgm_ops)"
    ))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX}"))
