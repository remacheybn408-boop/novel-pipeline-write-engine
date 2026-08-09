"""Add review and revision proposal records."""
from alembic import op
from sqlalchemy import Column, DateTime, String, Text, inspect

revision = "0012_review_revision"
down_revision = "0011_story_bible"
branch_labels = None
depends_on = None

def upgrade() -> None:
    if not inspect(op.get_bind()).has_table("revision_proposals"):
        op.create_table("revision_proposals", Column("id", String(64), primary_key=True), Column("chapter_id", String(64), nullable=False), Column("base_version_id", String(64), nullable=False), Column("before_hash", String(128), nullable=False), Column("after_text", Text, nullable=False), Column("after_hash", String(128), nullable=False), Column("rationale", Text, nullable=False), Column("status", String(32), nullable=False, server_default="PROPOSED"), Column("created_at", DateTime(timezone=True), nullable=False))

def downgrade() -> None:
    if inspect(op.get_bind()).has_table("revision_proposals"): op.drop_table("revision_proposals")
