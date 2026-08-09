from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.application.workflows.control import decode_checkpoint
from proseforge.domain.common.ids import new_id
from proseforge.domain.workflow.state import ALLOWED_TRANSITIONS
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.remaining import (
    WorkflowEventModel,
    WorkflowRunModel,
)
from proseforge.infrastructure.database.models.workflow_v2 import WorkflowNodeStateModel


class SqlAlchemyWorkflowRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, project_id: str, workflow_type: str, status: str = "QUEUED", cost_limit: float = 0.0, token_limit: int = 0) -> WorkflowRunModel:
        run = WorkflowRunModel(id=new_id(), project_id=project_id, workflow_type=workflow_type, status=status, cost_limit=cost_limit, token_limit=token_limit)
        self.session.add(run)
        await self.session.flush()
        await self.append_event(run.id, status, {"status": status})
        return run

    async def set_command(self, run: WorkflowRunModel, command: dict[str, object]) -> None:
        document = decode_checkpoint(run.checkpoint)
        document["command"] = command
        document.setdefault("phase", run.status)
        run.checkpoint = json.dumps(document, ensure_ascii=False)
        await self.session.flush()

    async def get_command(self, run: WorkflowRunModel) -> dict[str, object] | None:
        command = decode_checkpoint(run.checkpoint).get("command")
        return command if isinstance(command, dict) else None

    async def set_task(self, run: WorkflowRunModel, task_id: str) -> None:
        document = decode_checkpoint(run.checkpoint)
        document["active_task_id"] = task_id
        document["retry_count"] = int(document.get("retry_count", 0)) + 1
        run.checkpoint = json.dumps(document, ensure_ascii=False)
        await self.session.flush()

    async def get_owned(self, workflow_id: str, owner_id: str, *, lock: bool = False) -> WorkflowRunModel | None:
        query = (
            select(WorkflowRunModel)
            .join(ProjectModel, ProjectModel.id == WorkflowRunModel.project_id)
            .where(WorkflowRunModel.id == workflow_id, ProjectModel.owner_id == owner_id)
        )
        if lock:
            query = query.with_for_update()
        return await self.session.scalar(query)

    async def transition(self, run: WorkflowRunModel, status: str) -> None:
        if status not in ALLOWED_TRANSITIONS.get(run.status, set()):
            raise ValueError(f"invalid workflow transition: {run.status} -> {status}")
        run.status = status
        await self.append_event(run.id, status, {"status": status})

    async def acquire_lease(self, run: WorkflowRunModel, owner: str, ttl_seconds: int = 60) -> bool:
        now = datetime.now(UTC)
        # 租约身份按 run 归属（owner=celery:{run_id}）：pause/resume、retry 的
        # 继任任务携带同一 owner，必须能直接接管——否则前任暂停退出后租约残留，
        # 继任 lease-unavailable 退出且无再入队机制，run 永久卡死。
        if run.lease_owner and run.lease_owner != owner and run.lease_expires_at and run.lease_expires_at > now:
            return False
        run.lease_owner = owner
        run.lease_expires_at = now + timedelta(seconds=ttl_seconds)
        run.heartbeat_at = now
        await self.session.flush()
        return True

    async def heartbeat(self, run: WorkflowRunModel, owner: str, ttl_seconds: int = 60) -> None:
        if run.lease_owner != owner:
            raise PermissionError("workflow lease is not owned by caller")
        now = datetime.now(UTC)
        run.heartbeat_at = now
        run.lease_expires_at = now + timedelta(seconds=ttl_seconds)
        await self.session.flush()

    async def checkpoint(self, run: WorkflowRunModel, owner: str, checkpoint: str, estimated_cost: float = 0.0) -> None:
        if run.lease_owner != owner:
            raise PermissionError("workflow lease is not owned by caller")
        projected = float(run.estimated_cost or 0) + estimated_cost
        if run.cost_limit and projected > run.cost_limit:
            raise ValueError("workflow cost limit exceeded")
        document = decode_checkpoint(run.checkpoint)
        if "command" in document:
            document["phase"] = checkpoint
            run.checkpoint = json.dumps(document, ensure_ascii=False)
        else:
            run.checkpoint = checkpoint
        run.estimated_cost = projected
        await self.session.flush()

    async def recover_expired(self) -> int:
        now = datetime.now(UTC)
        rows = await self.session.scalars(select(WorkflowRunModel).where(WorkflowRunModel.status == "RUNNING", WorkflowRunModel.lease_expires_at <= now))
        recovered = 0
        for run in rows:
            run.status = "RECOVERING"
            run.lease_owner = None
            run.lease_expires_at = None
            await self.append_event(run.id, "RECOVERING", {"status": "RECOVERING", "reason": "lease_expired"})
            nodes = await self.session.scalars(select(WorkflowNodeStateModel).where(WorkflowNodeStateModel.run_id == run.id, WorkflowNodeStateModel.status == "RUNNING", WorkflowNodeStateModel.lease_expires_at <= now))
            for node in nodes:
                node.status = "PENDING"
                node.lease_owner = None
                node.lease_expires_at = None
                node.retry_count += 1
                node.updated_at = now
            recovered += 1
        await self.session.flush()
        return recovered

    async def reserve_node_budget(self, run: WorkflowRunModel, node: WorkflowNodeStateModel, estimated_tokens: int, estimated_cost: float) -> bool:
        locked_run = await self.session.scalar(select(WorkflowRunModel).where(WorkflowRunModel.id == run.id).with_for_update())
        locked_node = await self.session.scalar(select(WorkflowNodeStateModel).where(WorkflowNodeStateModel.id == node.id).with_for_update())
        if locked_run is None or locked_node is None:
            raise LookupError("workflow run or node not found")
        token_exceeded = bool(locked_run.token_limit and locked_run.used_tokens + estimated_tokens > locked_run.token_limit)
        cost_exceeded = bool(locked_run.cost_limit and float(locked_run.estimated_cost or 0) + estimated_cost > locked_run.cost_limit)
        if token_exceeded or cost_exceeded:
            locked_run.status = "BUDGET_BLOCKED"
            locked_node.status = "BLOCKED"
            await self.append_event(locked_run.id, "run.budget_blocked", {"status": "BUDGET_BLOCKED", "node_key": locked_node.node_key})
            await self.session.flush()
            return False
        locked_node.reserved_tokens = estimated_tokens
        locked_node.reserved_cost = estimated_cost
        locked_node.status = "RUNNING"
        locked_node.updated_at = datetime.now(UTC)
        await self.session.flush()
        return True

    async def append_event(self, workflow_id: str, event_type: str, payload: dict[str, object]) -> None:
        sequence = await self.session.scalar(
            update(WorkflowRunModel)
            .where(WorkflowRunModel.id == workflow_id)
            .values(event_cursor=WorkflowRunModel.event_cursor + 1)
            .returning(WorkflowRunModel.event_cursor)
        )
        if sequence is None:
            raise LookupError("workflow run not found")
        self.session.add(
            WorkflowEventModel(
                id=new_id(),
                workflow_run_id=workflow_id,
                sequence_no=int(sequence),
                event_type=event_type,
                payload=json.dumps(payload, ensure_ascii=False),
            )
        )
        await self.session.flush()

    async def events(self, workflow_id: str, after: int = 0) -> list[dict[str, object]]:
        rows = await self.session.scalars(
            select(WorkflowEventModel)
            .where(WorkflowEventModel.workflow_run_id == workflow_id, WorkflowEventModel.sequence_no > after)
            .order_by(WorkflowEventModel.sequence_no)
        )
        return [
            {"id": row.sequence_no, "event": row.event_type, "data": json.loads(row.payload)}
            for row in rows
        ]
