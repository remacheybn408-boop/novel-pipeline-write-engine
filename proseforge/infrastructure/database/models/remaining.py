from __future__ import annotations

from sqlalchemy import (
    Boolean,
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


class ProviderCredentialModel(Base):
    __tablename__ = "provider_credentials"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_provider_credentials_user_provider"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_payload: Mapped[str] = mapped_column(Text, nullable=False)


class ModelCatalogModel(Base):
    __tablename__ = "model_catalog"
    __table_args__ = (UniqueConstraint("provider", "model_id", name="uq_model_catalog_provider_model_id"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    capabilities: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # Owner of a manually registered row. NULL = synced row or legacy shared
    # manual row (pre-0050): visible to everyone, not deletable via API.
    owner_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )


class ModelProfileModel(Base):
    __tablename__ = "model_profiles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    config: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class AttachmentModel(Base):
    __tablename__ = "attachments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class ArtifactModel(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    artifact_type: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class ContextItemModel(Base):
    __tablename__ = "context_items"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provenance: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class ContextSnapshotModel(Base):
    __tablename__ = "context_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class OutlineModel(Base):
    __tablename__ = "outlines"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="UPLOADED")
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    missing_questions: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class OutlineVersionModel(Base):
    __tablename__ = "outline_versions"
    __table_args__ = (UniqueConstraint("outline_id", "version_no", name="uq_outline_versions_outline_id_version_no"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    outline_id: Mapped[str] = mapped_column(String(64), ForeignKey("outlines.id", ondelete="CASCADE"), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class WorkflowRunModel(Base):
    __tablename__ = "workflow_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    workflow_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checkpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimated_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    cost_limit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    used_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    token_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    event_cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkflowStepModel(Base):
    __tablename__ = "workflow_steps"
    __table_args__ = (UniqueConstraint("workflow_run_id", "idempotency_key", name="uq_workflow_steps_run_idempotency"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_run_id: Mapped[str] = mapped_column(String(64), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class WorkflowEventModel(Base):
    __tablename__ = "workflow_events"
    __table_args__ = (UniqueConstraint("workflow_run_id", "sequence_no", name="uq_workflow_events_run_sequence"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_run_id: Mapped[str] = mapped_column(String(64), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class ModelCallModel(Base):
    __tablename__ = "model_calls"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_run_id: Mapped[str] = mapped_column(String(64), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)


class QualityReportModel(Base):
    __tablename__ = "quality_reports"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    report: Mapped[str] = mapped_column(Text, nullable=False)


class HealthCheckModel(Base):
    __tablename__ = "health_checks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    component: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class AuditLogModel(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
