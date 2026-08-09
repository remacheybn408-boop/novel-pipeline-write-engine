"""Shared agent-run creation core.

Extracted from POST /api/v3/projects/{id}/agent-runs so the swarm chat
entry (conversations send_message with mode="swarm") creates runs through
exactly the same path: concurrency cap, idempotency replay, cluster model
resolution, policy snapshot, run.created event, commit, enqueue.

Caller must be inside ``async with uow:`` and must have already checked
project ownership/mode. Validation of the graph itself (TaskGraph rules,
budget sums) stays with the caller — it is payload-specific.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from proseforge.application.agents.policy_snapshot import (
    build_snapshot,
    canonical_json,
    sign,
)
from proseforge.application.models.cluster_config import (
    available_model_refs,
    get_effective_cluster_config,
    resolve_from_config,
)
from proseforge.domain.common.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.agents import (
    AgentEventModel,
    AgentPolicySnapshotModel,
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

# Single-user concurrent run cap (PENDING/RUNNING count), same value the
# route enforced before the extraction. The live value comes from settings
# (PROSEFORGE_MAX_ACTIVE_RUNS_PER_USER); this constant is only the default
# documented here for tests and readers.
MAX_ACTIVE_RUNS_PER_USER = 3


def max_active_runs_per_user() -> int:
    """Configured cap (settings-cached); tests run with the default 3."""
    from proseforge.settings import get_settings

    return get_settings().max_active_runs_per_user


class RunConcurrencyLimitError(ConflictError):
    """Active PENDING/RUNNING runs reached MAX_ACTIVE_RUNS_PER_USER."""

    code = "RUN_CONCURRENCY_LIMIT"
    retryable = True


class ClusterModelPoolError(ValidationError):
    """Cluster mode needs at least two models; the API answers 400."""

    status_code = 400


class QueueUnavailableError(DomainError):
    """Run row committed but the task queue rejected the enqueue."""

    code = "QUEUE_UNAVAILABLE"
    status_code = 503


@dataclass(frozen=True)
class RunTaskSpec:
    id: str
    role: str
    depends_on: tuple[str, ...] = ()
    token_budget: int = 1


async def _event(uow: SqlAlchemyUnitOfWork, run: AgentRunModel, event_type: str, payload: dict[str, object]) -> None:
    locked = await uow.session.scalar(
        select(AgentRunModel)
        .where(AgentRunModel.id == run.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise NotFoundError("agent run not found")
    sequence = locked.event_cursor + 1
    uow.session.add(AgentEventModel(id=new_id(), run_id=locked.id, sequence=sequence, event_type=event_type, payload=json.dumps(payload, ensure_ascii=False, sort_keys=True)))
    locked.event_cursor = sequence
    locked.updated_at = datetime.now(UTC)


def _audit_payload(user_id: str, run: AgentRunModel, action: str, decision: str, reason: str = "") -> dict[str, object]:
    # Audit vocabulary: actor/run/action/policy_version/resource/decision/
    # reason; never the raw goal or any manuscript text.
    return {
        "actor": user_id, "run_id": run.id, "task_id": None, "action": action,
        "policy_version": run.policy_version, "resource_id": run.id,
        "decision": decision, "reason": reason,
    }


async def create_agent_run(
    uow: SqlAlchemyUnitOfWork,
    queue,
    *,
    user_id: str,
    project_id: str,
    goal: str,
    tasks: list[RunTaskSpec],
    budget_limit: int,
    master_key: SecretStr,
    environment: str = "development",
    graph_revision: int = 1,
    chapter_id: str | None = None,
    base_version_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    idempotency_key: str | None = None,
    fault_mode: str | None = None,
    assistant_message_id: str | None = None,
    force_single_model: bool = False,
    settings=None,
) -> tuple[AgentRunModel, bool]:
    """Create + commit + enqueue an agent run. Returns (run, created);
    an idempotency replay returns the existing run with created=False.

    force_single_model (normal-mode dispatch): the request's provider/model
    stand as-is, the cluster config is not consulted, and the run row is
    marked single_model so the executor skips cluster resolution too —
    every lane collapses onto the one model the user selected.

    ``settings`` should carry the app's resolved Settings; None falls back
    to the process-wide get_settings() cache.
    """
    if fault_mode and environment.lower() in {"production", "prod"}:
        raise ValidationError("fault injection is disabled in production")
    if idempotency_key:
        existing = await uow.session.scalar(select(AgentRunModel).where(AgentRunModel.user_id == user_id, AgentRunModel.project_id == project_id, AgentRunModel.idempotency_key == idempotency_key))
        if existing is not None:
            return existing, False
    active = int(await uow.session.scalar(select(func.count(AgentRunModel.id)).where(AgentRunModel.user_id == user_id, AgentRunModel.status.in_(("PENDING", "RUNNING")))) or 0)
    if active >= max_active_runs_per_user():
        raise RunConcurrencyLimitError("maximum concurrent agent runs reached")
    if chapter_id is not None:
        chapter = await uow.chapters.get_owned(chapter_id, user_id)
        if chapter is None or chapter.project_id != project_id:
            raise NotFoundError("chapter not found")
    now = datetime.now(UTC)
    # Swarm model selection: with an effective cluster config (project
    # override > global) the request's provider/model are IGNORED — swarm
    # models come from the cluster config card only. The run row stores the
    # resolved write-role model purely as a display fallback; the executor
    # re-resolves the real model per task role. The writing-model lock does
    # not apply to swarm runs (locked=None). No config anywhere -> the
    # request values flow through (legacy).
    run_provider, run_model = provider, model
    effective_cluster = await get_effective_cluster_config(uow, user_id, project_id)
    if effective_cluster.source != "none" and not force_single_model:
        pool = await available_model_refs(uow, user_id)
        if effective_cluster.config.get("mode") == "cluster" and len(pool) < 2:
            raise ClusterModelPoolError("集群模式需至少添加 2 个模型")
        swarm_roles = resolve_from_config(
            effective_cluster.config, pool=pool, locked=None,
            requested=(provider or "openai", model or "gpt-4.1-mini"),
        )
        run_provider, run_model = swarm_roles.write
    run = AgentRunModel(id=new_id(), user_id=user_id, project_id=project_id, chapter_id=chapter_id, base_version_id=base_version_id, fault_mode=fault_mode, goal_hash=hashlib.sha256(goal.encode()).hexdigest(), goal=goal, provider=run_provider, model=run_model, idempotency_key=idempotency_key, single_model=True if force_single_model else None, graph_revision=graph_revision, status="PENDING", budget_limit=budget_limit, created_at=now, updated_at=now)
    uow.session.add(run)
    # Flush the parent first: the ORM does not order inserts by table-level
    # FK dependencies (no relationship() anywhere in this project), and
    # _event()'s SELECT FOR UPDATE triggers an autoflush that may insert
    # agent_policy_snapshots/agent_tasks before agent_runs. sqlite never
    # enforces the FK so tests stayed green; PostgreSQL rejects it outright
    # (production incident).
    await uow.session.flush()
    snapshot = build_snapshot()
    policy_json = canonical_json(snapshot)
    run.policy_version = str(snapshot["policy_version"])
    uow.session.add(AgentPolicySnapshotModel(id=new_id(), run_id=run.id, policy_version=run.policy_version, policy_hash=hashlib.sha256(policy_json.encode()).hexdigest(), payload=policy_json, signature=sign(snapshot, master_key)))
    for item in tasks:
        uow.session.add(AgentTaskModel(id=new_id(), run_id=run.id, task_key=item.id, role=item.role, status="PENDING", token_budget=item.token_budget, depends_on=json.dumps(list(item.depends_on)), checkpoint_id=None))
    await _event(uow, run, "run.created", {"graph_revision": graph_revision, "task_count": len(tasks), **_audit_payload(user_id, run, "create", "allow")})
    if assistant_message_id is not None:
        # Swarm chat entry: link the placeholder assistant message in the
        # SAME commit as the run. A separate commit used to leave a half
        # state (run exists, message has no agent_run_id -> stuck PENDING)
        # when the link step failed between the two commits.
        await uow.conversations.set_message_agent_run(assistant_message_id, run.id)
    try:
        await uow.commit()
    except IntegrityError:
        # Idempotency race: a partial unique index intercepted a concurrent
        # same-key insert; roll back, re-read by (user_id, idempotency_key)
        # and replay.
        await uow.rollback()
        if idempotency_key:
            existing = await uow.session.scalar(select(AgentRunModel).where(AgentRunModel.user_id == user_id, AgentRunModel.project_id == project_id, AgentRunModel.idempotency_key == idempotency_key))
            if existing is not None:
                return existing, False
        raise
    try:
        await queue.enqueue("proseforge.agents.execute_run", {"run_id": run.id, "user_id": user_id, "provider": provider, "model": model})
    except Exception as exc:
        run.status = "FAILED"
        run.terminal_reason = "queue unavailable"
        if assistant_message_id is not None:
            # The linked placeholder message must not outlive the failed run
            # as a PENDING orphan with no task and no event.
            await uow.conversations.set_message_status(assistant_message_id, "FAILED")
        await _event(uow, run, "run.queue_failed", {"error": type(exc).__name__})
        await uow.commit()
        raise QueueUnavailableError("agent queue unavailable") from exc
    return run, True
