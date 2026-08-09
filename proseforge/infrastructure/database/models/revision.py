from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from proseforge.infrastructure.database.base import Base


class RevisionProposalModel(Base):
    __tablename__ = "revision_proposals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chapter_id: Mapped[str] = mapped_column(String(64), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True)
    base_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    before_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    after_text: Mapped[str] = mapped_column(Text, nullable=False)
    after_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PROPOSED")
    hunks_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    affected_facts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    conflict_status: Mapped[str] = mapped_column(String(32), nullable=False, default="clear")
    guard_status: Mapped[str] = mapped_column(String(32), nullable=False, default="clear")
    context_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewReportModel(Base):
    __tablename__ = "review_reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    findings_json: Mapped[str] = mapped_column(Text, nullable=False)
    scores_json: Mapped[str] = mapped_column(Text, nullable=False)
    model_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    context_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    usage_call_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="COMPLETED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
