from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from proseforge.infrastructure.database.base import Base


class ModelUsageRecordModel(Base):
    __tablename__ = "model_usage_records"
    __table_args__ = (
        UniqueConstraint("call_id", name="uq_model_usage_records_call_id"),
        Index("ix_model_usage_records_user_created", "user_id", "created_at"),
        Index("ix_model_usage_records_project_created", "project_id", "created_at"),
        Index("ix_model_usage_records_conversation_created", "conversation_id", "created_at"),
        Index("ix_model_usage_records_workflow_created", "workflow_run_id", "created_at"),
        Index("ix_model_usage_records_provider_model_created", "provider", "model_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_step: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    call_id: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Durable high-water mark already rolled into workflow_runs.used_tokens.
    accounted_total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    usage_source: Mapped[str] = mapped_column(String(16), nullable=False, default="provider")
    is_final: Mapped[bool] = mapped_column(nullable=False, default=False)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    metadata_json: Mapped[str] = mapped_column("metadata", Text, nullable=False, default="{}")
