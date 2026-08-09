from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import func, select

from proseforge.application.workflows.definition_service import (
    WorkflowDefinitionService,
)
from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.remaining import (
    WorkflowEventModel,
    WorkflowRunModel,
)
from proseforge.infrastructure.database.models.workflow_v2 import WorkflowNodeStateModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}


class WorkflowRunService:
    def __init__(self, uow: SqlAlchemyUnitOfWork):
        self.uow = uow

    @property
    def session(self):
        if self.uow.session is None:
            raise RuntimeError("unit of work is not active")
        return self.uow.session

    async def create(self, definition_id: str, owner_id: str, token_limit: int = 0, cost_limit: float = 0) -> tuple[WorkflowRunModel, list[WorkflowNodeStateModel]]:
        definition = await WorkflowDefinitionService(self.uow).get(definition_id, owner_id)
        if definition is None:
            raise LookupError("workflow definition not found")
        document = json.loads(definition.definition_json)
        nodes = document["nodes"]
        reserved_tokens = sum(int((node.get("config") or node.get("params") or {}).get("reserved_tokens", (node.get("config") or node.get("params") or {}).get("token_budget", node.get("reserved_tokens", 0))) or 0) for node in nodes)
        reserved_cost = sum(float((node.get("config") or node.get("params") or {}).get("reserved_cost", (node.get("config") or node.get("params") or {}).get("cost_budget", node.get("reserved_cost", 0))) or 0) for node in nodes)
        blocked = bool((token_limit and reserved_tokens > token_limit) or (cost_limit and reserved_cost > cost_limit))
        run = await self.uow.workflows.create(definition.project_id, "DEFINITION", status="BUDGET_BLOCKED" if blocked else "RUNNING", cost_limit=cost_limit, token_limit=token_limit)
        run.checkpoint = json.dumps({"definition_id": definition.id, "definition_revision": definition.revision, "control_results": {}, "reserved_tokens": reserved_tokens, "reserved_cost": reserved_cost}, ensure_ascii=False)
        states: list[WorkflowNodeStateModel] = []
        now = datetime.now(UTC)
        for node in nodes:
            config = node.get("config") or node.get("params") or {}
            state = WorkflowNodeStateModel(id=new_id(), run_id=run.id, node_key=node["id"], status="BLOCKED" if blocked else "PENDING", retry_count=0, reserved_tokens=int(config.get("reserved_tokens", config.get("token_budget", node.get("reserved_tokens", 0))) or 0), used_tokens=0, reserved_cost=float(config.get("reserved_cost", config.get("cost_budget", node.get("reserved_cost", 0))) or 0), used_cost=0, updated_at=now)
            self.session.add(state)
            states.append(state)
        if blocked:
            await self.uow.workflows.append_event(run.id, "run.budget_blocked", {"status": "BUDGET_BLOCKED", "reserved_tokens": reserved_tokens, "reserved_cost": reserved_cost})
        else:
            await self.uow.workflows.append_event(run.id, "run.started", {"status": "RUNNING", "definition_id": definition.id, "revision": definition.revision})
        await self.session.flush()
        return run, states

    async def get_owned(self, run_id: str, owner_id: str, *, lock: bool = False) -> WorkflowRunModel | None:
        query = select(WorkflowRunModel).join(ProjectModel, ProjectModel.id == WorkflowRunModel.project_id).where(WorkflowRunModel.id == run_id, ProjectModel.owner_id == owner_id, WorkflowRunModel.workflow_type == "DEFINITION")
        if lock:
            query = query.with_for_update()
        return await self.session.scalar(query)

    async def snapshot(self, run_id: str, owner_id: str) -> tuple[WorkflowRunModel, list[WorkflowNodeStateModel], int] | None:
        run = await self.get_owned(run_id, owner_id)
        if run is None:
            return None
        nodes = list((await self.session.scalars(select(WorkflowNodeStateModel).where(WorkflowNodeStateModel.run_id == run.id).order_by(WorkflowNodeStateModel.node_key))).all())
        cursor = int(await self.session.scalar(select(func.coalesce(func.max(WorkflowEventModel.sequence_no), 0)).where(WorkflowEventModel.workflow_run_id == run.id)) or 0)
        return run, nodes, cursor

    async def control(self, run_id: str, owner_id: str, action: str, idempotency_key: str) -> tuple[WorkflowRunModel, bool]:
        run = await self.get_owned(run_id, owner_id, lock=True)
        if run is None:
            raise LookupError("workflow run not found")
        checkpoint = json.loads(run.checkpoint or "{}")
        results = checkpoint.setdefault("control_results", {})
        if idempotency_key in results:
            return run, True
        targets = {"pause": "PAUSED", "resume": "RUNNING", "cancel": "CANCELLED", "retry": "RUNNING"}
        if action not in targets:
            raise ValueError("unsupported workflow action")
        target = targets[action]
        allowed = {
            "pause": {"RUNNING", "QUEUED", "RECOVERING"},
            "resume": {"PAUSED", "BUDGET_BLOCKED", "RECOVERING"},
            "cancel": {"RUNNING", "QUEUED", "PAUSED", "RECOVERING", "BUDGET_BLOCKED", "FAILED"},
            "retry": {"FAILED", "PAUSED", "BUDGET_BLOCKED"},
        }[action]
        if run.status not in allowed:
            raise ValueError(f"cannot {action} workflow in {run.status} state")
        if action in {"resume", "retry"}:
            run.lease_owner = None
            run.lease_expires_at = None
        run.status = target
        if action == "retry":
            states = await self.session.scalars(select(WorkflowNodeStateModel).where(WorkflowNodeStateModel.run_id == run.id, WorkflowNodeStateModel.status.in_(["FAILED", "BLOCKED"])))
            for state in states:
                state.status = "PENDING"
                state.retry_count += 1
                state.lease_owner = None
                state.lease_expires_at = None
                state.updated_at = datetime.now(UTC)
        results[idempotency_key] = {"action": action, "status": target}
        run.checkpoint = json.dumps(checkpoint, ensure_ascii=False)
        event_name = {"pause": "run.paused", "resume": "run.resumed", "cancel": "run.cancelled", "retry": "run.retried"}[action]
        await self.uow.workflows.append_event(run.id, event_name, {"status": target, "idempotency_key": idempotency_key})
        await self.session.flush()
        return run, False


def run_response(run: WorkflowRunModel) -> dict[str, object]:
    checkpoint = json.loads(run.checkpoint or "{}")
    return {"id": run.id, "project_id": run.project_id, "workflow_type": run.workflow_type, "status": run.status, "definition_id": checkpoint.get("definition_id"), "definition_revision": checkpoint.get("definition_revision"), "token_limit": run.token_limit, "cost_limit": run.cost_limit}


def node_response(node: WorkflowNodeStateModel) -> dict[str, object]:
    return {"id": node.id, "node_key": node.node_key, "status": node.status, "checkpoint": json.loads(node.checkpoint_json) if node.checkpoint_json else None, "retry_count": node.retry_count, "reserved_tokens": node.reserved_tokens, "used_tokens": node.used_tokens, "reserved_cost": node.reserved_cost, "used_cost": node.used_cost}
