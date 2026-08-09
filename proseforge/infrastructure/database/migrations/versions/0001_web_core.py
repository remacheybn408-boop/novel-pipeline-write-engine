"""Create core project, manuscript, and conversation tables."""

import sqlalchemy as sa
from alembic import op

revision = "0001_web_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("slug", sa.String(200), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("genre", sa.String(200), nullable=False),
        sa.Column("style", sa.String(200), nullable=False),
        sa.Column("language", sa.String(32), nullable=False),
        sa.UniqueConstraint("owner_id", "slug", name="uq_projects_owner_id_slug"),
    )
    op.create_table(
        "chapters",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("chapter_no", sa.Integer, nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("active_version_id", sa.String(64)),
        sa.UniqueConstraint("project_id", "chapter_no", name="uq_chapters_project_id_chapter_no"),
    )
    op.create_index("ix_chapters_project_id", "chapters", ["project_id"])
    op.create_table(
        "chapter_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("chapter_id", sa.String(64), nullable=False),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("word_count", sa.Integer, nullable=False),
        sa.UniqueConstraint("chapter_id", "version_no", name="uq_chapter_versions_chapter_id_version_no"),
        sa.UniqueConstraint("chapter_id", "content_hash", name="uq_chapter_versions_chapter_id_content_hash"),
    )
    op.create_index("ix_chapter_versions_chapter_id", "chapter_versions", ["chapter_id"])
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
    )
    op.create_index("ix_conversations_project_id", "conversations", ["project_id"])
    op.create_table(
        "conversation_branches",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("conversation_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("parent_branch_id", sa.String(64)),
        sa.Column("forked_from_message_id", sa.String(64)),
    )
    op.create_index("ix_conversation_branches_conversation_id", "conversation_branches", ["conversation_id"])
    op.create_table(
        "messages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("branch_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("client_request_id", sa.String(128)),
        sa.Column("sequence_no", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.UniqueConstraint("client_request_id", name="uq_messages_client_request_id"),
    )
    op.create_index("ix_messages_branch_id", "messages", ["branch_id"])
    op.create_table(
        "message_chunks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("message_id", sa.String(64), nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.UniqueConstraint("message_id", "chunk_index", name="uq_message_chunks_message_id_chunk_index"),
    )
    op.create_index("ix_message_chunks_message_id", "message_chunks", ["message_id"])
    op.create_table(
        "conversation_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("conversation_id", sa.String(64), nullable=False),
        sa.Column("event_sequence", sa.Integer, nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.UniqueConstraint("conversation_id", "event_sequence", name="uq_conversation_events_conversation_id_event_sequence"),
    )
    op.create_index("ix_conversation_events_conversation_id", "conversation_events", ["conversation_id"])


def downgrade() -> None:
    for table in ("conversation_events", "message_chunks", "messages", "conversation_branches", "conversations", "chapter_versions", "chapters", "projects"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
