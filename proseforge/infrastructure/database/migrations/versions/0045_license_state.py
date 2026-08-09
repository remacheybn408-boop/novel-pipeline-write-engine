"""license_state + license_free_usage tables and users.created_at.

Client-side license integration (L3): the single-row license_state caches
the enrolled API key (encrypted), the last verified certificate and the
monotonic handshake marker for grace computation; license_free_usage is
the local pre-deducted free-cluster quota cache (the center ledger stays
authoritative). users.created_at gives the handshake payload a real
registration timestamp. Plain columns only, PG/SQLite portable,
inspector-guarded and idempotent (same style as 0043/0044).
"""

import sqlalchemy as sa
from alembic import op

revision = "0045_license_state"
down_revision = "0044_run_single_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "license_state" not in tables:
        op.create_table(
            "license_state",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("api_key_encrypted", sa.Text(), nullable=True),
            sa.Column("certificate_json", sa.Text(), nullable=True),
            sa.Column("signature", sa.Text(), nullable=True),
            sa.Column("last_server_time", sa.String(64), nullable=True),
            sa.Column("last_handshake_monotonic", sa.Float(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "license_free_usage" not in tables:
        op.create_table(
            "license_free_usage",
            sa.Column("user_id", sa.String(64), primary_key=True),
            sa.Column("free_cluster_remaining", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("pending_usage", sa.Integer(), nullable=False, server_default="0"),
        )
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "created_at" not in user_columns:
        op.add_column("users", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "created_at" in user_columns:
        op.drop_column("users", "created_at")
    if "license_free_usage" in tables:
        op.drop_table("license_free_usage")
    if "license_state" in tables:
        op.drop_table("license_state")
