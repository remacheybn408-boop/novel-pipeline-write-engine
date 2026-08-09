"""Add versioned structured story bible facts."""
from alembic import op
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    inspect,
)

revision = "0011_story_bible"
down_revision = "0010_conversation_message_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not inspect(op.get_bind()).has_table("story_bible_entries"):
        op.create_table("story_bible_entries", Column("id", String(64), primary_key=True), Column("project_id", String(64), nullable=False), Column("kind", String(32), nullable=False), Column("key", String(200), nullable=False), Column("value_json", Text, nullable=False), Column("status", String(32), nullable=False, server_default="active"), Column("confidence", Float, nullable=False, server_default="1"), Column("source", String(128), nullable=False, server_default="user"), Column("pinned", Boolean, nullable=False, server_default="0"), Column("version", Integer, nullable=False, server_default="1"), Column("created_at", DateTime(timezone=True), nullable=False), Column("updated_at", DateTime(timezone=True), nullable=False), UniqueConstraint("project_id", "kind", "key", "version", name="uq_story_bible_version"))


def downgrade() -> None:
    if inspect(op.get_bind()).has_table("story_bible_entries"):
        op.drop_table("story_bible_entries")
