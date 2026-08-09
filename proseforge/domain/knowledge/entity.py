from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from proseforge.domain.common.ids import new_id


@dataclass(frozen=True)
class KnowledgeDocument:
    """Project-scoped knowledge base document (reserved CRUD skeleton)."""

    id: str
    project_id: str
    title: str
    content: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def create(cls, *, project_id: str, title: str, content: str = "") -> KnowledgeDocument:
        return cls(new_id(), project_id, title, content)
