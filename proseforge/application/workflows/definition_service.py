from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import delete, select

from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.workflow_v2 import (
    WorkflowDefinitionModel,
)
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

ALLOWED_NODE_KINDS = {"intake", "plan", "write", "review", "rewrite", "export"}


def validate_definition(definition: dict[str, object]) -> dict[str, object]:
    nodes = definition.get("nodes")
    edges = definition.get("edges")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("definition must contain at least one node")
    if not isinstance(edges, list):
        raise ValueError("definition edges must be a list")  # noqa: TRY004 -- ValueError is the contract: the definitions route catches it and maps to 422

    node_ids: set[str] = set()
    for raw in nodes:
        if not isinstance(raw, dict):
            raise ValueError("each node must be an object")  # noqa: TRY004 -- ValueError is the contract: the definitions route catches it and maps to 422
        node_id = raw.get("id")
        kind = raw.get("kind", raw.get("type"))
        if not isinstance(node_id, str) or not node_id.strip() or node_id in node_ids:
            raise ValueError("node ids must be unique non-empty strings")
        if kind not in ALLOWED_NODE_KINDS:
            raise ValueError(f"unsupported node kind: {kind}")
        node_ids.add(node_id)

    adjacency = {node_id: [] for node_id in node_ids}
    for raw in edges:
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            source, target = raw
        elif isinstance(raw, dict):
            source, target = raw.get("source"), raw.get("target")
        else:
            raise ValueError("each edge must be an object")
        if source not in node_ids or target not in node_ids:
            raise ValueError("edge references an unknown node")
        if source == target:
            raise ValueError("self-loop edges are not allowed")
        adjacency[str(source)].append(str(target))

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError("workflow graph must be acyclic")
        if node_id in visited:
            return
        visiting.add(node_id)
        for target in adjacency[node_id]:
            visit(target)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in node_ids:
        visit(node_id)
    return definition


class WorkflowDefinitionService:
    def __init__(self, uow: SqlAlchemyUnitOfWork):
        self.uow = uow

    @property
    def session(self):
        if self.uow.session is None:
            raise RuntimeError("unit of work is not active")
        return self.uow.session

    async def _project_owned(self, project_id: str, owner_id: str) -> bool:
        return bool(await self.session.scalar(select(ProjectModel.id).where(ProjectModel.id == project_id, ProjectModel.owner_id == owner_id)))

    async def create(self, project_id: str, owner_id: str, name: str, definition: dict[str, object]) -> WorkflowDefinitionModel:
        if not await self._project_owned(project_id, owner_id):
            raise LookupError("project not found")
        existing = await self.session.scalar(select(WorkflowDefinitionModel.id).where(WorkflowDefinitionModel.project_id == project_id, WorkflowDefinitionModel.name == name))
        if existing:
            raise FileExistsError("workflow definition name already exists")
        return await self._insert(project_id, name, 1, validate_definition(definition))

    async def _insert(self, project_id: str, name: str, revision: int, definition: dict[str, object]) -> WorkflowDefinitionModel:
        now = datetime.now(UTC)
        row = WorkflowDefinitionModel(id=new_id(), project_id=project_id, name=name, revision=revision, definition_json=json.dumps(definition, ensure_ascii=False), created_at=now, updated_at=now)
        self.session.add(row)
        await self.session.flush()
        return row

    async def list(self, project_id: str, owner_id: str) -> list[WorkflowDefinitionModel]:
        if not await self._project_owned(project_id, owner_id):
            raise LookupError("project not found")
        rows = list((await self.session.scalars(select(WorkflowDefinitionModel).where(WorkflowDefinitionModel.project_id == project_id).order_by(WorkflowDefinitionModel.name, WorkflowDefinitionModel.revision.desc()))).all())
        latest: dict[str, WorkflowDefinitionModel] = {}
        for row in rows:
            latest.setdefault(row.name, row)
        return list(latest.values())

    async def get(self, definition_id: str, owner_id: str) -> WorkflowDefinitionModel | None:
        return await self.session.scalar(select(WorkflowDefinitionModel).join(ProjectModel, ProjectModel.id == WorkflowDefinitionModel.project_id).where(WorkflowDefinitionModel.id == definition_id, ProjectModel.owner_id == owner_id))

    async def update(self, definition_id: str, owner_id: str, name: str | None, definition: dict[str, object]) -> WorkflowDefinitionModel:
        current = await self.get(definition_id, owner_id)
        if current is None:
            raise LookupError("workflow definition not found")
        return await self._insert(current.project_id, name or current.name, current.revision + 1, validate_definition(definition))

    async def delete(self, definition_id: str, owner_id: str) -> None:
        current = await self.get(definition_id, owner_id)
        if current is None:
            raise LookupError("workflow definition not found")
        await self.session.execute(delete(WorkflowDefinitionModel).where(WorkflowDefinitionModel.project_id == current.project_id, WorkflowDefinitionModel.name == current.name))


def definition_response(row: WorkflowDefinitionModel) -> dict[str, object]:
    return {"id": row.id, "project_id": row.project_id, "name": row.name, "revision": row.revision, "definition": json.loads(row.definition_json)}
