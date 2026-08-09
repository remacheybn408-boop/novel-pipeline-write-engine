from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from proseforge.infrastructure.database.base import Base


class ToolCallLogModel(Base):
    """One row per tool invocation (or cache reuse) from chat tool rounds.

    Full result text is deliberately NOT stored — result_summary is truncated
    and result_bytes records the real size.
    """

    __tablename__ = "tool_call_log"
    __table_args__ = (
        Index("ix_tool_call_log_conversation_created", "conversation_id", "created_at"),
        Index("ix_tool_call_log_tool_created", "tool_name", "created_at"),
    )

    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    params_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    result_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resource_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
