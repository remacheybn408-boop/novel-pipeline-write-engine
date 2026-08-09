"""Add outline intake persistence and context controls."""

from alembic import op
from sqlalchemy import Boolean, Column, Integer, Text, inspect

from proseforge.infrastructure.database.base import Base

revision = "0005_outline_context"
down_revision = "0004_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())
    # Repair an installation whose revision marker advanced while table creation
    # was interrupted.  Creating only missing metadata tables is idempotent and
    # preserves any user data already present.
    for name, table in Base.metadata.tables.items():
        if name not in existing:
            table.create(bind=bind, checkfirst=True)
    columns = {column["name"] for column in inspect(bind).get_columns("context_items")}
    additions = (
        ("pinned", Boolean(), "false"),
        ("priority", Integer(), "0"),
        ("excluded", Boolean(), "false"),
        ("provenance", Text(), "{}"),
    )
    for name, column_type, default in additions:
        if name not in columns:
            op.add_column("context_items", Column(name, column_type, nullable=False, server_default=default))


def downgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("context_items")}
    for name in ("provenance", "excluded", "priority", "pinned"):
        if name in columns:
            op.drop_column("context_items", name)
    bind = op.get_bind()
    for name in ("outline_versions", "outlines"):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
