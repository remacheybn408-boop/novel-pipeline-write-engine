"""model_catalog.owner_id — per-user ownership of manually registered models.

Manual (user-added) catalog rows used to be global: visible to every user
and undeletable. This column records the owning user for new manual rows;
GET /api/v1/models scopes manual rows to their owner, and
DELETE /api/v1/models/{provider}/{model_id} removes only owned rows.

Legacy semantics: rows created before this migration keep owner_id = NULL.
A NULL owner on a manual row means "legacy shared" — still visible to all
users (backward compatible) and not deletable through the API. Synced
(discovery) rows also keep owner_id = NULL; they are filtered by the
caller's credentials as before.

Nullable VARCHAR(64) FK -> users.id, ON DELETE SET NULL (deleting a user
turns their manual rows into legacy shared rows instead of blocking the
delete or orphan-violating). Inspector-guarded and idempotent, same style
as 0042/0049.
"""

import sqlalchemy as sa
from alembic import op

revision = "0050_model_catalog_owner"
down_revision = "0049_paragraph_anchors"
branch_labels = None
depends_on = None

_INDEX = "ix_model_catalog_owner_id"


def upgrade() -> None:
    bind = op.get_bind()
    if "model_catalog" not in set(sa.inspect(bind).get_table_names()):
        # Fresh install: 0002 skips model_catalog (its ORM model has an
        # owner_id FK -> users.id and users only exists since 0004), so the
        # table is created here from current metadata — owner_id and its FK
        # come along inline, no follow-up ALTER needed.
        from proseforge.infrastructure.database.base import Base

        Base.metadata.tables["model_catalog"].create(bind=bind)
    existing = {column["name"] for column in sa.inspect(bind).get_columns("model_catalog")}
    if "owner_id" not in existing:
        # Batch mode: SQLite cannot ADD COLUMN with a FK constraint via plain
        # ALTER; on PostgreSQL batch mode degrades to a plain ADD COLUMN.
        with op.batch_alter_table("model_catalog") as batch:
            batch.add_column(sa.Column("owner_id", sa.String(64), nullable=True))
            batch.create_foreign_key(
                "fk_model_catalog_owner_id",
                "users",
                ["owner_id"],
                ["id"],
                ondelete="SET NULL",
            )
    existing_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("model_catalog")}
    if _INDEX not in existing_indexes:
        op.create_index(_INDEX, "model_catalog", ["owner_id"])


def downgrade() -> None:
    bind = op.get_bind()
    existing_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("model_catalog")}
    if _INDEX in existing_indexes:
        op.drop_index(_INDEX, table_name="model_catalog")
    existing = {column["name"] for column in sa.inspect(bind).get_columns("model_catalog")}
    if "owner_id" in existing:
        with op.batch_alter_table("model_catalog") as batch:
            batch.drop_column("owner_id")
