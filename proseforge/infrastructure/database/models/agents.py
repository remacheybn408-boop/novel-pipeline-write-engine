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


class AgentRunModel(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    chapter_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    base_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    proposal_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    fault_mode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    goal_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)  # 目标原文（0027 起写入；存量 run 为 NULL，prompt 回退 goal_hash 前缀）
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 缺省 None → executor 用 openai
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 缺省 None → executor 用 gpt-4.1-mini
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    single_model: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # True → 所有车道坍缩到 run 行模型（普通模式调度），executor 跳过集群解析
    graph_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    checkpoint_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    budget_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    budget_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v3-policy-1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentTaskModel(Base):
    __tablename__ = "agent_tasks"
    __table_args__ = (UniqueConstraint("run_id", "task_key", name="uq_agent_tasks_run_key"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    task_key: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    depends_on: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    checkpoint_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Retryable provider errors (5xx/429/timeout) use their own counter +
    # scheduled retry time: they never consume the `attempts` budget and the
    # claim loop skips the task until `next_attempt_at` (exponential backoff).
    retryable_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentEventModel(Base):
    __tablename__ = "agent_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_agent_events_run_sequence"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class AgentArtifactModel(Base):
    __tablename__ = "agent_artifacts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(100), nullable=False)
    sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v1")
    provenance: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    preview: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class AgentReviewModel(Base):
    __tablename__ = "agent_reviews"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewer_role: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    conflict_group: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class AgentMemoryModel(Base):
    __tablename__ = "agent_memories"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    # Deliberately no FK on run_id: PROJECT_WIDE_RUN ("") is a sentinel for
    # project-wide memories with no backing agent_runs row (memory_service).
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(200), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source_artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")


class AgentPolicySnapshotModel(Base):
    __tablename__ = "agent_policy_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    signature: Mapped[str | None] = mapped_column(String(128), nullable=True)


class AgentEvaluationModel(Base):
    __tablename__ = "agent_evaluations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    fixture_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class AgentGraphRevisionModel(Base):
    # 与迁移 0018 的 agent_graph_revisions 表对齐（表无 run_id 列；
    # run 归属与 expansion 元数据放在 payload JSON 里）。
    __tablename__ = "agent_graph_revisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    graph_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
