from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from proseforge.infrastructure.database.base import Base


class ConversationModel(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)


class ConversationBranchModel(Base):
    __tablename__ = "conversation_branches"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_branch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    forked_from_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fork_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MessageModel(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("user_id", "client_request_id", name="uq_messages_client_request_id"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    branch_id: Mapped[str] = mapped_column(String(64), ForeignKey("conversation_branches.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    client_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="COMPLETED")
    parent_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generation_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Swarm link (migration 0042): the placeholder assistant message an agent
    # run writes its summary back to. No FK on purpose (run lifecycle is
    # independent; deleting a run must not cascade into messages).
    agent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class MessageEditModel(Base):
    __tablename__ = "message_edits"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(64), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    original_content: Mapped[str] = mapped_column(Text, nullable=False)
    edited_content: Mapped[str] = mapped_column(Text, nullable=False)
    created_branch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MessageChunkModel(Base):
    __tablename__ = "message_chunks"
    __table_args__ = (UniqueConstraint("message_id", "chunk_index", name="uq_message_chunks_message_id_chunk_index"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(64), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)


class ConversationEventModel(Base):
    __tablename__ = "conversation_events"
    __table_args__ = (UniqueConstraint("conversation_id", "event_sequence", name="uq_conversation_events_conversation_id_event_sequence"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Polymorphic stream key, NOT always a conversations.id: DatabaseEventStream
    # stores both conversation: and message: topic suffixes here (see
    # infrastructure/events/database.py), so no FK is possible.
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
