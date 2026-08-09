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


class WorkflowDefinitionModel(Base):
    __tablename__ = "workflow_definitions"
    __table_args__ = (UniqueConstraint("project_id", "name", "revision", name="uq_workflow_definitions_project_name_revision"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkflowNodeStateModel(Base):
    __tablename__ = "workflow_node_states"
    __table_args__ = (UniqueConstraint("run_id", "node_key", name="uq_workflow_node_states_run_node_key"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    node_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    checkpoint_json: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    used_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
