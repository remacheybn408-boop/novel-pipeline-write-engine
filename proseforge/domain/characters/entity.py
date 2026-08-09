from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from proseforge.domain.common.ids import new_id


@dataclass(frozen=True)
class Character:
    id: str
    project_id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    summary: str = ""
    role: str = ""
    first_seen_chapter: int | None = None
    last_seen_chapter: int | None = None
    status: str = "active"  # active | archived
    source: str = "user"  # user | auto
    confidence: float = 1.0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def create(cls, *, project_id: str, name: str, aliases: list[str] | None = None, summary: str = "", role: str = "", source: str = "user", confidence: float = 1.0) -> Character:
        return cls(new_id(), project_id, name, aliases or [], summary, role, source=source, confidence=confidence)
