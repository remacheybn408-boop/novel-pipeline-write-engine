"""Character table (migration 0039).

Characters are project-scoped; auto-extracted rows (source="auto",
confidence 0.6) come from the chapter summarizer, user rows are authored
via the CRUD API and always win on merge.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from proseforge.infrastructure.database.base import Base


class CharacterModel(Base):
    __tablename__ = "characters"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_characters_project_name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    aliases_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    role: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    first_seen_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")  # active | archived
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="user")  # user | auto
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
