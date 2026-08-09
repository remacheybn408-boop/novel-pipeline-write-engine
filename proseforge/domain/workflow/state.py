from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

ALLOWED_TRANSITIONS = {
    "CREATED": {"WAITING_USER", "QUEUED", "CANCELLED"}, "WAITING_USER": {"QUEUED", "CANCELLED"},
    "QUEUED": {"RUNNING", "CANCELLED"}, "RUNNING": {"PAUSED", "RETRYING", "COMPLETED", "FAILED", "CANCELLED", "RECOVERING"},
    "PAUSED": {"QUEUED", "CANCELLED"}, "RETRYING": {"RUNNING", "FAILED", "PAUSED"}, "RECOVERING": {"QUEUED", "PAUSED", "FAILED"},
    "COMPLETED": set(), "FAILED": {"QUEUED"}, "CANCELLED": set(),
    "BUDGET_BLOCKED": {"QUEUED", "CANCELLED"},
}


class InvalidWorkflowTransition(ValueError):
    pass


@dataclass
class WorkflowState:
    status: str = "CREATED"

    def transition(self, target: str) -> None:
        if target not in ALLOWED_TRANSITIONS.get(self.status, set()):
            raise InvalidWorkflowTransition(f"{self.status} -> {target}")
        self.status = target


@dataclass
class WorkflowRun:
    id: str
    state: WorkflowState = None  # type: ignore[assignment]
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    checkpoint: str | None = None

    def __post_init__(self) -> None:
        if self.state is None:
            self.state = WorkflowState()

    def acquire_lease(self, owner: str, ttl_seconds: int = 60, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        # 同一 owner（按 run 归属）可重入接管：pause/resume、retry 接力语义；
        # 仅不同 owner 且租约未过期时互斥。与 SqlAlchemyWorkflowRepository 同语义。
        if self.lease_owner and self.lease_owner != owner and self.lease_expires_at and self.lease_expires_at > current:
            return False
        self.lease_owner = owner
        self.lease_expires_at = current + timedelta(seconds=ttl_seconds)
        self.heartbeat_at = current
        return True

    def heartbeat(self, owner: str, ttl_seconds: int = 60, now: datetime | None = None) -> None:
        if self.lease_owner != owner or not self.lease_expires_at:
            raise PermissionError("workflow lease is not owned by caller")
        current = now or datetime.now(UTC)
        self.heartbeat_at = current
        self.lease_expires_at = current + timedelta(seconds=ttl_seconds)

    def recover_if_expired(self, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        if self.lease_expires_at and self.lease_expires_at <= current and self.state.status == "RUNNING":
            self.state.transition("RECOVERING")
            self.lease_owner = None
            self.lease_expires_at = None
