from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from proseforge.domain.common.ids import new_id


@dataclass(frozen=True)
class Skill:
    id: str
    user_id: str
    name: str
    content: str
    description: str = ""
    enabled: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def create(cls, *, user_id: str, name: str, content: str, description: str = "", enabled: bool = True) -> Skill:
        return cls(id=new_id(), user_id=user_id, name=name, content=content, description=description, enabled=enabled)


@dataclass(frozen=True)
class McpServer:
    id: str
    user_id: str
    name: str
    transport: str  # streamable-http | sse
    url: str
    encrypted_headers: str | None = None
    enabled: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def create(cls, *, user_id: str, name: str, transport: str, url: str, encrypted_headers: str | None = None, enabled: bool = True, record_id: str | None = None) -> McpServer:
        return cls(id=record_id or new_id(), user_id=user_id, name=name, transport=transport, url=url, encrypted_headers=encrypted_headers, enabled=enabled)


@dataclass(frozen=True)
class BuiltinSkillState:
    id: str
    user_id: str
    skill_key: str
    enabled: bool
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
