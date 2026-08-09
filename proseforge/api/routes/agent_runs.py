from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import func, select

from proseforge.api.dependencies import current_user, require_work_project, unit_of_work

# --- WS-D：chief-proposal 端点依赖（独立分组，便于并行 workstream 合并） ---
from proseforge.application.agents.chief_handler import run_chief_proposal
from proseforge.application.agents.create_run import (
    RunConcurrencyLimitError,
    RunTaskSpec,
    create_agent_run,
    max_active_runs_per_user,
)
from proseforge.application.agents.expand_graph import (
    ExpansionChild,
    ExpansionPlan,
    TaskRowView,
    graph_hash_for,
    validate_expansion,
)
from proseforge.application.agents.memory_service import (
    MEMORY_STATUSES,
    decide_memory,
    invalidate_memory_slice_cache,
    list_memories,
    memory_view,
)
from proseforge.application.agents.policy_snapshot import verify
from proseforge.application.agents.review_handlers import snapshot_review
from proseforge.application.agents.validate_graph import validate_graph
from proseforge.application.auth.service import AuthUser
from proseforge.application.models.cluster_config import agent_role_to_cluster_role
from proseforge.domain.agents.task_graph import AgentTaskSpec, TaskGraph
from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.agents import (
    AgentArtifactModel,
    AgentEvaluationModel,
    AgentEventModel,
    AgentGraphRevisionModel,
    AgentMemoryModel,
    AgentPolicySnapshotModel,
    AgentReviewModel,
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.models.revision import RevisionProposalModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v3", tags=["agent-runs"])


class GraphTaskRequest(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=64)
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    output_artifact_type: str = Field(default="report", max_length=100)
    token_budget: int = Field(default=1, ge=1, le=12000)


class AgentRunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=12000)
    graph_revision: int = Field(default=1, ge=1)
    tasks: list[GraphTaskRequest] = Field(default_factory=list, max_length=64)
    budget_limit: int = Field(default=12000, ge=1, le=100000)
    chapter_id: str | None = None
    base_version_id: str | None = None
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=200)
    fault_mode: Literal["provider_timeout", "malformed_json", "budget_exhaustion", "crash_after_artifact_commit"] | None = None


class ArtifactRequest(BaseModel):
    task_id: str | None = None
    artifact_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, object] = Field(default_factory=dict)
    provenance: dict[str, object] = Field(default_factory=dict)
    preview: str = Field(default="", max_length=500)


class ReviewRequest(BaseModel):
    artifact_id: str
    reviewer_role: str
    status: str = Field(pattern=r"^(PASS|WARNING|CONFLICT|UNSUPPORTED)$")
    evidence: list[dict[str, object]] = Field(default_factory=list, max_length=32)
    conflict_group: str | None = None


def _run_response(run: AgentRunModel) -> dict[str, object]:
    return {
        "id": run.id, "project_id": run.project_id, "status": run.status,
        "goal_hash": run.goal_hash, "graph_revision": run.graph_revision,
        "checkpoint_id": run.checkpoint_id, "budget_used": run.budget_used,
        "budget_limit": run.budget_limit, "event_cursor": run.event_cursor,
        "policy_version": run.policy_version, "terminal_reason": run.terminal_reason,
        "chapter_id": run.chapter_id, "base_version_id": run.base_version_id, "proposal_id": run.proposal_id,
        "fault_mode": run.fault_mode,
    }


def _envelope(code: str, message: str, request_id: str = "", *, retryable: bool = False) -> dict[str, object]:
    # 公共错误封套，形状与 api/errors.py 的 domain_error_handler 一致。
    return {"error": {"code": code, "message": message, "retryable": retryable, "request_id": request_id, "details": {}}}


def _audit_payload(user_id: str, run: AgentRunModel, action: str, decision: str, reason: str = "", task_id: str | None = None) -> dict[str, object]:
    # 审计词汇：actor/run/task/action/policy_version/resource/decision/reason；绝不放原始目标或正文。
    return {
        "actor": user_id, "run_id": run.id, "task_id": task_id, "action": action,
        "policy_version": run.policy_version, "resource_id": run.id,
        "decision": decision, "reason": reason,
    }


async def _owned_run(uow: SqlAlchemyUnitOfWork, run_id: str, user_id: str) -> AgentRunModel:
    run = await uow.session.scalar(select(AgentRunModel).where(AgentRunModel.id == run_id, AgentRunModel.user_id == user_id))
    if run is None:
        raise HTTPException(status_code=404, detail="agent run not found")
    return run


async def _active_run_count(uow: SqlAlchemyUnitOfWork, user_id: str) -> int:
    # Active = PENDING/RUNNING, same definition as create_run.py's cap check.
    return int(await uow.session.scalar(select(func.count(AgentRunModel.id)).where(AgentRunModel.user_id == user_id, AgentRunModel.status.in_(("PENDING", "RUNNING")))) or 0)


async def _event(uow: SqlAlchemyUnitOfWork, run: AgentRunModel, event_type: str, payload: dict[str, object] | None = None) -> None:
    locked = await uow.session.scalar(
        select(AgentRunModel)
        .where(AgentRunModel.id == run.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise HTTPException(status_code=404, detail="agent run not found")
    sequence = locked.event_cursor + 1
    uow.session.add(AgentEventModel(id=new_id(), run_id=locked.id, sequence=sequence, event_type=event_type, payload=json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)))
    locked.event_cursor = sequence
    locked.updated_at = datetime.now(UTC)


async def _policy_snapshot_valid(uow: SqlAlchemyUnitOfWork, run: AgentRunModel, master_key: SecretStr) -> bool:
    row = await uow.session.scalar(select(AgentPolicySnapshotModel).where(AgentPolicySnapshotModel.run_id == run.id).order_by(AgentPolicySnapshotModel.id).limit(1))
    if row is None or row.policy_version != run.policy_version:
        return False
    try:
        snapshot = json.loads(row.payload)
    except ValueError:
        return False
    return verify(snapshot, row.signature, master_key)


@router.post("/projects/{project_id}/agent-runs", status_code=status.HTTP_201_CREATED)
async def start_run(
    project_id: str,
    payload: AgentRunRequest,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    tasks = payload.tasks or [GraphTaskRequest(id="planner", role="chief_planner"), GraphTaskRequest(id="reviewer", role="continuity_reviewer", depends_on=["planner"])]
    graph = TaskGraph(payload.graph_revision, tuple(AgentTaskSpec(item.id, item.role, tuple(item.depends_on), output_artifact_type=item.output_artifact_type, token_budget=item.token_budget) for item in tasks))
    try:
        validate_graph(graph)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if sum(item.token_budget for item in tasks) > payload.budget_limit:
        raise HTTPException(status_code=422, detail="graph token budget exceeds run budget")
    async with uow:
        if payload.fault_mode and request.app.state.settings.environment.lower() in {"production", "prod"}:
            raise HTTPException(status_code=422, detail="fault injection is disabled in production")
        await require_work_project(uow, user.id, project_id)
        try:
            # Shared creation core (concurrency cap, idempotency replay,
            # cluster model resolution, policy snapshot, enqueue) — the
            # swarm chat entry creates runs through the same function.
            run, _created = await create_agent_run(
                uow, request.app.state.queue,
                user_id=user.id, project_id=project_id, goal=payload.goal,
                tasks=[RunTaskSpec(id=item.id, role=item.role, depends_on=tuple(item.depends_on), token_budget=item.token_budget) for item in tasks],
                budget_limit=payload.budget_limit,
                master_key=request.app.state.settings.master_key,
                environment=request.app.state.settings.environment,
                graph_revision=payload.graph_revision,
                chapter_id=payload.chapter_id, base_version_id=payload.base_version_id,
                provider=payload.provider, model=payload.model,
                idempotency_key=idempotency_key, fault_mode=payload.fault_mode,
                settings=request.app.state.settings,
            )
        except RunConcurrencyLimitError:
            return JSONResponse(status_code=409, content=_envelope("RUN_CONCURRENCY_LIMIT", "maximum concurrent agent runs reached", getattr(request.state, "correlation_id", ""), retryable=True))
        return _run_response(run)


@router.get("/agent-runs/{run_id}")
async def get_run(run_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        run = await _owned_run(uow, run_id, user.id)
        # Quality-gate outcome for the write pipeline: the latest
        # gate.evaluated event payload; None when never evaluated.
        gate_row = await uow.session.scalar(
            select(AgentEventModel.payload)
            .where(AgentEventModel.run_id == run.id, AgentEventModel.event_type == "gate.evaluated")
            .order_by(AgentEventModel.sequence.desc())
            .limit(1)
        )
        gate: bool | None = None
        if gate_row is not None:
            try:
                gate = bool(json.loads(gate_row).get("passed"))
            except (ValueError, AttributeError):
                gate = None
        return {**_run_response(run), "gate": gate}


async def _control(run_id: str, action: str, user: AuthUser, uow: SqlAlchemyUnitOfWork, master_key: SecretStr, task_id: str | None = None, queue=None, request_id: str = "") -> dict[str, object]:
    run = await _owned_run(uow, run_id, user.id)
    # 策略快照签名校验（fail-closed）：快照被篡改或签名缺失一律拒绝控制动作并留审计事件。
    if not await _policy_snapshot_valid(uow, run, master_key):
        await _event(uow, run, "run.policy_violation", _audit_payload(user.id, run, action, "deny", "policy snapshot signature mismatch", task_id))
        await uow.commit()
        return JSONResponse(status_code=409, content=_envelope("POLICY_VIOLATION", "policy snapshot signature mismatch", request_id))
    # resume only from PAUSED: a PENDING run is already queued, so allowing
    # resume there would double-enqueue it (two executors claiming one run);
    # FAILED goes through retry.
    transitions = {"pause": ("PAUSED", {"PENDING", "RUNNING"}), "resume": ("RUNNING", {"PAUSED"}), "cancel": ("CANCELLED", {"PENDING", "RUNNING", "PAUSED", "FAILED", "BUDGET_EXHAUSTED"})}
    if action == "retry":
        # RUNNING 不在允许集：在飞 run 重试会产生重复执行；BUDGET_EXHAUSTED 允许重试脱困。
        if run.status not in {"FAILED", "PAUSED", "BUDGET_EXHAUSTED"}:
            return JSONResponse(status_code=409, content=_envelope("RUN_NOT_RETRYABLE", "run is not retryable", request_id))
        # Re-check the active-run cap before re-enqueueing: without it a
        # pause/retry cycle bypasses the per-user active-run cap.
        if await _active_run_count(uow, user.id) >= max_active_runs_per_user():
            return JSONResponse(status_code=409, content=_envelope("RUN_CONCURRENCY_LIMIT", "maximum concurrent agent runs reached", request_id, retryable=True))
        if task_id:
            task = await uow.session.scalar(select(AgentTaskModel).where(AgentTaskModel.id == task_id, AgentTaskModel.run_id == run.id))
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            tasks_to_retry = [task]
        else:
            # Run-level retry: reset every FAILED task back to PENDING so the
            # executor has work to pick up; without this the run flips
            # straight back to FAILED (no PENDING, some FAILED).
            tasks_to_retry = list(await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run.id, AgentTaskModel.status == "FAILED")))
        for retry_task in tasks_to_retry:
            retry_task.status, retry_task.last_error = "PENDING", None
            if task_id:
                retry_task.attempts = retry_task.attempts + 1
            else:
                # Run 级 retry = 新一次脱困周期：两级尝试计数与退避排程清零。
                # 旧语义 attempts+1 让有效不可重试次数（attempts −
                # retryable_attempts）在 retry 后仍触顶，首次失败即 FAILED。
                retry_task.attempts = 0
                retry_task.retryable_attempts = 0
                retry_task.next_attempt_at = None
        previous = run.status
        budget_adjustment: dict[str, int] | None = None
        if previous == "BUDGET_EXHAUSTED":
            # 熔断脱困必须带新预算空间：budget_used 保持记账语义（不回零），
            # budget_limit ×1.5 取整上调（不低于 used+1），否则重跑必然在同一
            # 任务再次熔断。executor 首轮的 lane 账本重算只升不降，不会吃掉上调。
            old_limit = int(run.budget_limit or 0)
            new_limit = max(int(old_limit * 1.5), int(run.budget_used or 0) + 1)
            run.budget_limit = new_limit
            budget_adjustment = {"old_limit": old_limit, "new_limit": new_limit, "budget_used": int(run.budget_used or 0)}
        run.status = "RUNNING"
        retry_payload = _audit_payload(user.id, run, action, "allow", f"{previous}->RUNNING", task_id)
        if budget_adjustment is not None:
            retry_payload["budget_adjustment"] = budget_adjustment
        await _event(uow, run, "run.retry", retry_payload)
    else:
        target, allowed = transitions[action]
        if run.status == target:
            return _run_response(run)
        if action == "resume" and run.status == "FAILED":
            # FAILED 只允许 retry 重新入队，resume 一律 409。
            return JSONResponse(status_code=409, content=_envelope("INVALID_RUN_TRANSITION", "failed runs can only be retried, not resumed", request_id))
        if action == "resume" and run.status == "PENDING":
            # Already queued; resuming would double-enqueue the run.
            return JSONResponse(status_code=409, content=_envelope("INVALID_RUN_TRANSITION", "pending runs are already queued; resume is only valid from PAUSED", request_id))
        if run.status not in allowed:
            return JSONResponse(status_code=409, content=_envelope("INVALID_RUN_TRANSITION", "invalid run transition", request_id))
        # Re-check the active-run cap before re-enqueueing: without it a
        # pause/resume cycle bypasses the per-user active-run cap.
        if action == "resume" and await _active_run_count(uow, user.id) >= max_active_runs_per_user():
            return JSONResponse(status_code=409, content=_envelope("RUN_CONCURRENCY_LIMIT", "maximum concurrent agent runs reached", request_id, retryable=True))
        previous = run.status
        run.status = target
        if target == "CANCELLED":
            run.terminal_reason = "cancelled by user"
        await _event(uow, run, f"run.{action}", _audit_payload(user.id, run, action, "allow", f"{previous}->{target}"))
    await uow.commit()
    if action in {"resume", "retry"} and queue is not None:
        try:
            await queue.enqueue("proseforge.agents.execute_run", {"run_id": run.id, "user_id": user.id})
        except Exception:
            # Enqueue failed after the status commit: roll the run back to
            # its pre-control status so it never wedges in RUNNING with no
            # executor (same failure pattern as create_run.py's enqueue).
            run.status = previous
            await _event(uow, run, "run.queue_failed", _audit_payload(user.id, run, action, "deny", "queue unavailable", task_id))
            await uow.commit()
            return JSONResponse(status_code=503, content=_envelope("QUEUE_UNAVAILABLE", "agent queue unavailable", request_id, retryable=True))
    return _run_response(run)


def _make_control_handler(action: str):
    async def handler(
        run_id: str,
        request: Request,
        user: Annotated[AuthUser, Depends(current_user)],
        uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
    ):
        async with uow:
            return await _control(run_id, action, user, uow, request.app.state.settings.master_key, queue=request.app.state.queue, request_id=getattr(request.state, "correlation_id", ""))

    handler.__name__ = f"{action}_agent_run"
    return handler


for _action in ("pause", "resume", "cancel"):
    router.add_api_route(f"/agent-runs/{{run_id}}/{_action}", _make_control_handler(_action), methods=["POST"])


@router.post("/agent-runs/{run_id}/retry")
async def retry_run(run_id: str, request: Request, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)], task_id: str | None = None) -> dict[str, object]:
    async with uow:
        return await _control(run_id, "retry", user, uow, request.app.state.settings.master_key, task_id, request.app.state.queue, getattr(request.state, "correlation_id", ""))


@router.get("/agent-runs/{run_id}/tasks")
async def list_tasks(run_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> list[dict[str, object]]:
    async with uow:
        await _owned_run(uow, run_id, user.id)
        rows = await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id).order_by(AgentTaskModel.id))
        return [{"id": row.id, "task_key": row.task_key, "role": row.role, "cluster_role": agent_role_to_cluster_role(row.role, row.task_key), "status": row.status, "attempts": row.attempts, "token_budget": row.token_budget, "depends_on": json.loads(row.depends_on), "last_error": row.last_error} for row in rows]


@router.get("/agent-runs/{run_id}/events")
async def list_events(run_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)], after: int = 0) -> dict[str, object]:
    async with uow:
        await _owned_run(uow, run_id, user.id)
        rows = await uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == run_id, AgentEventModel.sequence > max(0, after)).order_by(AgentEventModel.sequence))
        events = [{"id": row.id, "sequence": row.sequence, "event": row.event_type, "data": json.loads(row.payload)} for row in rows]
        return {"events": events, "next_cursor": events[-1]["sequence"] if events else max(0, after)}


@router.post("/agent-runs/{run_id}/artifacts", status_code=status.HTTP_201_CREATED)
async def create_artifact(run_id: str, payload: ArtifactRequest, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        run = await _owned_run(uow, run_id, user.id)
        raw = json.dumps(payload.payload, ensure_ascii=False, sort_keys=True).encode()
        artifact = AgentArtifactModel(id=new_id(), run_id=run.id, task_id=payload.task_id, artifact_type=payload.artifact_type, sha256=hashlib.sha256(raw).hexdigest(), provenance=json.dumps(payload.provenance, sort_keys=True), preview=payload.preview, payload=json.dumps(payload.payload, ensure_ascii=False))
        uow.session.add(artifact)
        await _event(uow, run, "artifact.committed", {"artifact_id": artifact.id, "sha256": artifact.sha256})
        await uow.commit()
        return {"id": artifact.id, "artifact_type": artifact.artifact_type, "sha256": artifact.sha256, "preview": artifact.preview, "provenance": json.loads(artifact.provenance)}


@router.get("/agent-runs/{run_id}/artifacts")
async def list_artifacts(run_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> list[dict[str, object]]:
    async with uow:
        await _owned_run(uow, run_id, user.id)
        rows = await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.run_id == run_id).order_by(AgentArtifactModel.id))
        return [{"id": row.id, "artifact_type": row.artifact_type, "sha256": row.sha256, "preview": row.preview, "provenance": json.loads(row.provenance)} for row in rows]


@router.post("/agent-runs/{run_id}/reviews", status_code=status.HTTP_201_CREATED)
async def create_review(run_id: str, payload: ReviewRequest, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        await _owned_run(uow, run_id, user.id)
        artifact = await uow.session.scalar(select(AgentArtifactModel).where(AgentArtifactModel.id == payload.artifact_id, AgentArtifactModel.run_id == run_id))
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        if not payload.evidence and payload.status != "UNSUPPORTED":
            raise HTTPException(status_code=422, detail="review evidence is required")
        review = AgentReviewModel(id=new_id(), run_id=run_id, artifact_id=payload.artifact_id, reviewer_role=payload.reviewer_role, status=payload.status, evidence=json.dumps(payload.evidence), conflict_group=payload.conflict_group, payload="{}")
        uow.session.add(review)
        await uow.commit()
        return {"id": review.id, "artifact_id": payload.artifact_id, "status": review.status, "evidence": payload.evidence, "conflict_group": review.conflict_group}


@router.get("/agent-runs/{run_id}/reviews")
async def list_reviews(run_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> list[dict[str, object]]:
    async with uow:
        await _owned_run(uow, run_id, user.id)
        rows = await uow.session.scalars(select(AgentReviewModel).where(AgentReviewModel.run_id == run_id).order_by(AgentReviewModel.id))
        return [{"id": row.id, "artifact_id": row.artifact_id, "reviewer_role": row.reviewer_role, "status": row.status, "evidence": json.loads(row.evidence), "conflict_group": row.conflict_group} for row in rows]


@router.get("/agent-runs/{run_id}/audit")
async def audit_run(run_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> list[dict[str, object]]:
    async with uow:
        await _owned_run(uow, run_id, user.id)
        rows = await uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == run_id).order_by(AgentEventModel.sequence))
        return [{"sequence": row.sequence, "event": row.event_type, "payload": json.loads(row.payload)} for row in rows]


@router.post("/agent-runs/{run_id}/chief-proposal")
async def chief_proposal(run_id: str, request: Request, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    """WS-D（蓝图 V3-007）：手动触发 Chief Editor 合并提案。

    owner-only（跨用户 404）；策略快照签名校验（fail-closed，篡改 409 并留审计事件）；
    run 必须 COMPLETED；run.proposal_id 幂等（已存在直接返回既有提案，不重复创建）。
    返回 {"proposal_id", "guard_status"}；新建 201，幂等回放 200。
    """
    async with uow:
        run = await _owned_run(uow, run_id, user.id)
        if not await _policy_snapshot_valid(uow, run, request.app.state.settings.master_key):
            await _event(uow, run, "run.policy_violation", _audit_payload(user.id, run, "chief-proposal", "deny", "policy snapshot signature mismatch"))
            await uow.commit()
            return JSONResponse(status_code=409, content=_envelope("POLICY_VIOLATION", "policy snapshot signature mismatch", getattr(request.state, "correlation_id", "")))
        if run.status != "COMPLETED":
            return JSONResponse(status_code=409, content=_envelope("RUN_NOT_COMPLETED", "run must be COMPLETED before chief proposal", getattr(request.state, "correlation_id", "")))
        if run.proposal_id:
            existing = await uow.session.get(RevisionProposalModel, run.proposal_id)
            if existing is not None:
                return {"proposal_id": existing.id, "guard_status": existing.guard_status}
        if not run.chapter_id or not run.base_version_id:
            raise HTTPException(status_code=422, detail="run has no chapter_id/base_version_id; chief proposal requires a chapter target")
        reviews = [snapshot_review(row) for row in await uow.session.scalars(select(AgentReviewModel).where(AgentReviewModel.run_id == run.id))]
        try:
            result = await run_chief_proposal(uow, run.id, reviews)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await uow.commit()
        return JSONResponse(status_code=status.HTTP_201_CREATED if result["created"] else status.HTTP_200_OK, content={"proposal_id": result["proposal_id"], "guard_status": result["guard_status"]})


# --- WS-E：动态扩展 + 作用域记忆 + 评估（蓝图 V3-005 memory / V3-008） ---
# 端点只追加在本文件末尾；变更类动作一律先过 _policy_snapshot_valid（fail-closed）。


class ExpansionChildRequest(BaseModel):
    task_key: str | None = Field(default=None, max_length=128)
    role: str = Field(min_length=1, max_length=64)
    depends_on: list[str] | None = Field(default=None, max_length=8)
    input_artifact_types: list[str] = Field(default_factory=list, max_length=8)
    output_artifact_type: str = Field(default="report", max_length=100)
    token_budget: int = Field(default=1, ge=1, le=12000)
    permission_profile: str = Field(default="default", max_length=64)


class ExpansionRequest(BaseModel):
    children: list[ExpansionChildRequest] = Field(min_length=1, max_length=8)
    expansion_reason: str = Field(min_length=1, max_length=500)
    dedupe_key: str = Field(min_length=1, max_length=200)


class GraphValidateRequest(ExpansionRequest):
    parent_task_id: str = Field(min_length=1, max_length=128)


class MemoryDecisionRequest(BaseModel):
    decision: Literal["accept", "reject"]
    memory_ids: list[str] = Field(default_factory=list, max_length=64)


async def _task_views(uow: SqlAlchemyUnitOfWork, run_id: str) -> list[TaskRowView]:
    rows = await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id).order_by(AgentTaskModel.id))
    return [TaskRowView(task_key=row.task_key, role=row.role, status=row.status, depends_on=tuple(json.loads(row.depends_on)), token_budget=row.token_budget) for row in rows]


async def _expansion_plan(uow: SqlAlchemyUnitOfWork, run: AgentRunModel, payload: ExpansionRequest) -> ExpansionPlan:
    # 历史扩展记录（agent_graph_revisions 无 run_id 列，按 payload.run_id 过滤）供 dedupe/reason 去重
    rows = await uow.session.scalars(select(AgentGraphRevisionModel).where(AgentGraphRevisionModel.project_id == run.project_id).order_by(AgentGraphRevisionModel.id))
    priors: list[dict[str, object]] = []
    for row in rows:
        try:
            data = json.loads(row.payload)
        except ValueError:
            continue
        if data.get("run_id") == run.id:
            priors.append(data)
    return ExpansionPlan(
        children=tuple(
            ExpansionChild(
                role=item.role, task_key=item.task_key or None,
                depends_on=tuple(item.depends_on) if item.depends_on is not None else None,
                input_artifact_types=tuple(item.input_artifact_types),
                output_artifact_type=item.output_artifact_type,
                token_budget=item.token_budget, permission_profile=item.permission_profile,
            )
            for item in payload.children
        ),
        expansion_reason=payload.expansion_reason,
        dedupe_key=payload.dedupe_key,
        prior_expansions=tuple(priors),
    )


@router.post("/agent-runs/{run_id}/graph/validate")
async def validate_run_graph(run_id: str, payload: GraphValidateRequest, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    """扩展 dry-run：与 expand 同一套校验，不落库。"""
    async with uow:
        run = await _owned_run(uow, run_id, user.id)
        parent = await uow.session.scalar(select(AgentTaskModel).where(AgentTaskModel.id == payload.parent_task_id, AgentTaskModel.run_id == run.id))
        plan = await _expansion_plan(uow, run, payload)
        violations = validate_expansion(
            tasks=await _task_views(uow, run.id),
            parent_task_key=parent.task_key if parent is not None else payload.parent_task_id,
            plan=plan, budget_limit=run.budget_limit, budget_used=run.budget_used,
        )
        return {"valid": not violations, "violations": violations}


@router.post("/agent-runs/{run_id}/tasks/{task_id}/expand", status_code=status.HTTP_201_CREATED)
async def expand_run_task(run_id: str, task_id: str, payload: ExpansionRequest, request: Request, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]):
    """用户会话触发的图扩展：模型输出不能直接建任务，只有本端点能落新任务行。"""
    async with uow:
        run = await _owned_run(uow, run_id, user.id)
        if not await _policy_snapshot_valid(uow, run, request.app.state.settings.master_key):
            await _event(uow, run, "run.policy_violation", _audit_payload(user.id, run, "expand", "deny", "policy snapshot signature mismatch", task_id))
            await uow.commit()
            return JSONResponse(status_code=409, content=_envelope("POLICY_VIOLATION", "policy snapshot signature mismatch", getattr(request.state, "correlation_id", "")))
        parent = await uow.session.scalar(select(AgentTaskModel).where(AgentTaskModel.id == task_id, AgentTaskModel.run_id == run.id))
        if parent is None:
            raise HTTPException(status_code=404, detail="task not found")
        plan = await _expansion_plan(uow, run, payload)
        tasks = await _task_views(uow, run.id)
        violations = validate_expansion(tasks=tasks, parent_task_key=parent.task_key, plan=plan, budget_limit=run.budget_limit, budget_used=run.budget_used)
        if violations:
            content = _envelope("EXPANSION_INVALID", "expansion validation failed", getattr(request.state, "correlation_id", ""))
            content["error"]["details"]["violations"] = violations
            return JSONResponse(status_code=422, content=content)
        created: list[AgentTaskModel] = []
        for index, child in enumerate(plan.children):
            key = child.task_key or f"{parent.task_key}-expand-{index + 1}"
            depends = list(child.depends_on) if child.depends_on is not None else [parent.task_key]
            row = AgentTaskModel(id=new_id(), run_id=run.id, task_key=key, role=child.role, status="PENDING", token_budget=child.token_budget, depends_on=json.dumps(depends), checkpoint_id=None)
            uow.session.add(row)
            created.append(row)
        revision = run.graph_revision + 1
        new_views = tasks + [TaskRowView(task_key=row.task_key, role=row.role, status=row.status, depends_on=tuple(json.loads(row.depends_on)), token_budget=row.token_budget) for row in created]
        uow.session.add(AgentGraphRevisionModel(
            id=new_id(), project_id=run.project_id, revision=revision, graph_hash=graph_hash_for(new_views),
            payload=json.dumps({"run_id": run.id, "parent_task_key": parent.task_key, "dedupe_key": plan.dedupe_key, "expansion_reason": plan.expansion_reason, "added_task_keys": [row.task_key for row in created], "revision": revision}, ensure_ascii=False, sort_keys=True),
        ))
        run.graph_revision = revision
        run.updated_at = datetime.now(UTC)
        await _event(uow, run, "graph.expanded", {**_audit_payload(user.id, run, "expand", "allow", plan.expansion_reason, task_id), "graph_revision": revision, "dedupe_key": plan.dedupe_key, "added_task_keys": [row.task_key for row in created]})
        await uow.commit()
        return {
            "graph_revision": revision, "dedupe_key": plan.dedupe_key,
            "tasks": [{"id": row.id, "task_key": row.task_key, "role": row.role, "status": row.status, "token_budget": row.token_budget, "depends_on": json.loads(row.depends_on)} for row in created],
        }


@router.post("/agent-runs/{run_id}/artifacts/{artifact_id}/accept")
async def accept_run_artifact(run_id: str, artifact_id: str, payload: MemoryDecisionRequest, request: Request, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]):
    """用户审批：把该 Artifact 来源的 PENDING 记忆候选翻成 ACCEPTED/REJECTED。

    这是唯一的事实激活通道——Agent 角色的 activate_memory_fact 能力在
    domain/agents/policy.py 恒拒，模型输出无法自行激活记忆。
    """
    async with uow:
        run = await _owned_run(uow, run_id, user.id)
        if not await _policy_snapshot_valid(uow, run, request.app.state.settings.master_key):
            await _event(uow, run, "run.policy_violation", _audit_payload(user.id, run, "accept_artifact", "deny", "policy snapshot signature mismatch"))
            await uow.commit()
            return JSONResponse(status_code=409, content=_envelope("POLICY_VIOLATION", "policy snapshot signature mismatch", getattr(request.state, "correlation_id", "")))
        artifact = await uow.session.scalar(select(AgentArtifactModel).where(AgentArtifactModel.id == artifact_id, AgentArtifactModel.run_id == run.id))
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        candidates = list(await uow.session.scalars(select(AgentMemoryModel).where(AgentMemoryModel.run_id == run.id, AgentMemoryModel.source_artifact_id == artifact_id, AgentMemoryModel.status == "PENDING")))
        if payload.memory_ids:
            wanted = set(payload.memory_ids)
            if wanted - {row.id for row in candidates}:
                raise HTTPException(status_code=404, detail="memory not found")
            candidates = [row for row in candidates if row.id in wanted]
        for row in candidates:
            decide_memory(row, payload.decision)
        invalidate_memory_slice_cache()  # 审批立即可见于本进程后续记忆切片
        event_type = "memory.activated" if payload.decision == "accept" else "memory.rejected"
        await _event(uow, run, event_type, {**_audit_payload(user.id, run, "accept_artifact", "allow", f"{payload.decision} via artifact"), "artifact_id": artifact_id, "memory_decision": payload.decision, "memory_ids": [row.id for row in candidates], "updated": len(candidates)})
        await uow.commit()
        return {"artifact_id": artifact_id, "decision": payload.decision, "updated": len(candidates), "memories": [memory_view(row) for row in candidates]}


@router.get("/agent-runs/{run_id}/metrics")
async def get_run_metrics(run_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    """运行度量：预算/任务计数/时长/评估分数；不含任何正文内容。"""
    async with uow:
        run = await _owned_run(uow, run_id, user.id)
        counts = await uow.session.execute(select(AgentTaskModel.status, func.count(AgentTaskModel.id)).where(AgentTaskModel.run_id == run.id).group_by(AgentTaskModel.status))
        task_counts = {str(status_value): int(count) for status_value, count in counts}
        evaluations = await uow.session.scalars(select(AgentEvaluationModel).where(AgentEvaluationModel.run_id == run.id).order_by(AgentEvaluationModel.id))
        return {
            "run_id": run.id, "status": run.status, "graph_revision": run.graph_revision,
            "budget_used": run.budget_used, "budget_limit": run.budget_limit,
            "task_counts": task_counts, "task_total": sum(task_counts.values()),
            "event_cursor": run.event_cursor,
            "duration_ms": max(0, int((run.updated_at - run.created_at).total_seconds() * 1000)),
            "evaluations": [{"id": row.id, "fixture_hash": row.fixture_hash, "score": row.score, "payload": json.loads(row.payload)} for row in evaluations],
        }


@router.get("/agent-runs/{run_id}/memories")
async def list_run_memories(run_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)], status: str | None = None) -> list[dict[str, object]]:
    async with uow:
        run = await _owned_run(uow, run_id, user.id)
        normalized = status.upper() if status else None
        if normalized is not None and normalized not in MEMORY_STATUSES:
            raise HTTPException(status_code=422, detail="unknown memory status")
        rows = await list_memories(uow.session, project_id=run.project_id, run_id=run.id, status=normalized)
        return [memory_view(row) for row in rows]


def _zip_member_name(artifact: dict[str, object], payload: dict[str, object], used: set[str]) -> str:
    """Zip layout: outline.md for chapter plans, scenes/ for drafts,
    reviews/ for reviewer reports, <artifact_type>/ for anything else."""
    task_key = str(artifact.get("task_key") or "artifact")
    if "chapters" in payload:
        name = "outline.md"
    elif "content" in payload:
        name = f"scenes/{task_key}.md"
    elif "findings" in payload:
        name = f"reviews/{task_key}.md"
    else:
        name = f"{artifact.get('artifact_type', 'misc')}/{task_key}.md"
    suffix = 2
    candidate = name
    while candidate in used:  # deterministic dedupe for repeated task keys
        stem, dot, ext = name.rpartition(".")
        candidate = f"{stem}-{suffix}{dot}{ext}"
        suffix += 1
    used.add(candidate)
    return candidate


@router.get("/agent-runs/{run_id}/export.zip")
async def export_run_zip(run_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]):
    """Run deliverables as an in-memory zip: summary.md (same renderer as
    the swarm message writeback) + every artifact as markdown."""
    import io
    import zipfile

    from fastapi.responses import Response

    from proseforge.application.agents.run_summary import (
        artifact_markdown,
        infer_intent,
        render_run_summary,
    )

    async with uow:
        run = await _owned_run(uow, run_id, user.id)
        task_rows = list(await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run.id)))
        task_key_by_id = {row.id: row.task_key for row in task_rows}
        artifact_rows = list(await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.run_id == run.id)))
        task_dicts = [{"task_key": row.task_key, "role": row.role, "status": row.status, "last_error": row.last_error} for row in task_rows]
        artifact_dicts = [{"task_key": task_key_by_id.get(row.task_id, ""), "artifact_type": row.artifact_type, "preview": row.preview, "payload": row.payload} for row in artifact_rows]
        # Mirror the message writeback (agent_executor.writeback_run_message):
        # feed the gate outcome and chapter writeback into the summary so the
        # exported summary.md matches the chat rendering.
        gate_info: dict | None = None
        chapter_info: dict | None = None
        for event_row in await uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == run.id, AgentEventModel.event_type.in_(("gate.evaluated", "chapter.written_back"))).order_by(AgentEventModel.sequence)):
            event_data = json.loads(event_row.payload)
            if event_row.event_type == "gate.evaluated":
                gate_info = event_data
            else:
                chapter_info = event_data
        if gate_info and not gate_info.get("passed") and run.status == "COMPLETED":
            # Post-rewrite re-check of the FINAL draft (rewrite wins, then
            # select / legacy scene) — feeds the "带警告交付" warning line.
            from proseforge.application.agents.quality_gate import evaluate_gate

            parsed: dict[str, list[dict]] = {}
            for item in artifact_dicts:
                try:
                    payload = json.loads(str(item.get("payload") or "{}"))
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    parsed.setdefault(str(item.get("task_key", "")), []).append(payload)
            final_payload = next(
                (p for key in ("rewrite", "select", "scene") for p in parsed.get(key, []) if isinstance(p.get("content"), str) and p["content"].strip()),
                None,
            )
            post = evaluate_gate(goal=run.goal or "", scene_payload=final_payload, review_payloads=parsed.get("recheck", []))
            gate_info = {**gate_info, "post_passed": post.passed, "post_reasons": post.reasons}
        summary = render_run_summary(intent=infer_intent(task_dicts), status=run.status, terminal_reason=run.terminal_reason, tasks=task_dicts, artifacts=artifact_dicts, gate=gate_info, chapter=chapter_info)
        buffer = io.BytesIO()
        used_names: set[str] = {"summary.md"}
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("summary.md", summary)
            for artifact in artifact_dicts:
                from proseforge.application.agents.run_summary import _payload_dict

                payload = _payload_dict(artifact)
                archive.writestr(_zip_member_name(artifact, payload, used_names), artifact_markdown(artifact))
        return Response(
            content=buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="proseforge-run-{run_id[:8]}.zip"'},
        )
