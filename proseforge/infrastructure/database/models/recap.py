"""Memory-pyramid recap rollups (migration 0047).

One row per (project, level, span_start): ``volume`` recaps compress a
volume's chapter summaries, ``book`` recaps roll volume recaps up
incrementally, ``era`` recaps roll up every 10 volumes. ``stale`` is the
invalidation flag a later task flips when a covered chapter is revised;
producers always rewrite content and clear the flag on regeneration.
"""

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
)
from sqlalchemy.orm import Mapped, mapped_column

from proseforge.infrastructure.database.base import Base


class RecapRollupModel(Base):
    __tablename__ = "recap_rollups"
    __table_args__ = (
        UniqueConstraint("project_id", "level", "span_start", name="uq_recap_rollups_project_level_span_start"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False)  # volume | book | era
    span_start: Mapped[int] = mapped_column(Integer, nullable=False)
    span_end: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # JSON list of source ids (chapter version ids for volume level, source
    # rollup ids for book/era), stored as text like the other *_json columns.
    source_version_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
