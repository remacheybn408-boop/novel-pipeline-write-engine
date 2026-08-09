from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from proseforge.domain.common.ids import new_id


@dataclass(frozen=True)
class Conversation:
    id: str
    project_id: str
    title: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    archived: bool = False

    @classmethod
    def create(cls, project_id: str, title: str = "Untitled conversation") -> Conversation:
        return cls(id=new_id(), project_id=project_id, title=title)


@dataclass(frozen=True)
class ConversationBranch:
    id: str
    conversation_id: str
    name: str
    parent_branch_id: str | None = None
    forked_from_message_id: str | None = None
    status: str = "ACTIVE"
    title: str | None = None


@dataclass(frozen=True)
class Message:
    id: str
    branch_id: str
    role: str
    content: str
    client_request_id: str | None = None
    status: str = "COMPLETED"
    parent_message_id: str | None = None
    generation_attempt: int = 1
    model_snapshot: dict | None = None
    reasoning_snapshot: dict | None = None
    content_hash: str | None = None
    agent_run_id: str | None = None


@dataclass(frozen=True)
class MessageChunk:
    id: str
    message_id: str
    chunk_index: int
    event_type: str
    content: str
