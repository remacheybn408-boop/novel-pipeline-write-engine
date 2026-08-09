from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from proseforge.infrastructure.database.base import Base


class ProjectModel(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("owner_id", "slug"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    genre: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    style: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    language: Mapped[str] = mapped_column(String(32), default="zh-CN", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)
    mode: Mapped[str] = mapped_column(String(8), default="work", server_default="work", nullable=False)
    # Writing-model lock (migration 0038). Nullable redundancy only: the lock
    # is an application-layer rule, the columns stay editable so a future
    # "switch model + re-read" flow can rewrite them without a migration.
    writing_model_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    writing_model_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model_lock_source: Mapped[str | None] = mapped_column(String(16), nullable=True)  # outline_import | first_chapter
    # Per-project cluster config override (migration 0041). NULL = follow the
    # global cluster preference; same stored shape as the global preference.
    cluster_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
