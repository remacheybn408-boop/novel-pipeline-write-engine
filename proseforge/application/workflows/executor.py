"""V2 workflow node executor (V2-008).

Walks a run's pinned definition graph in topological order and executes one
node per transaction: PENDING → RUNNING (node lease) → reserve node budget →
invoke the node kind's application-layer use case → record actual usage on
node + run → release the unused reservation → COMPLETED (checkpoint_json) or
FAILED.  Between nodes the run row is re-read so PAUSED / CANCELLED take
effect at node boundaries; budget overrun reuses the repository's
BUDGET_BLOCKED pause path.  The terminal event is committed in the same
transaction as the terminal status, so SSE replay can never observe a
finished run without its final event.

Node-kind mappings (thin but honest — v2 definitions carry no provider
configuration yet, so no node calls an LLM; used_tokens/used_cost therefore
stay 0 unless a handler performs metered work):

- intake:  OutlineIntakeService parses the configured outline payload and
  records missing fields / clarification questions.
- plan:    chapter_planner.plan_chapters expands volumes/chapters_per_volume
  into the planned chapter list.
- write:   commits the configured content as a new active ChapterVersion.
- review:  RuleQualityService over the chapter's active version (no guards
  configured for v2 yet — the PASS decision is recorded with the reviewed
  content hash; a BLOCK decision fails the node).
- rewrite: commits the configured revised content as a new active version.
- export:  render_export over all active chapter versions + an export
  manifest row (file_sha256 / byte_size), same as the exports route.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from proseforge.application.outlines.intake_service import OutlineIntakeService
from proseforge.application.planning.chapter_planner import plan_chapters
from proseforge.application.quality.rule_quality_service import RuleQualityService
from proseforge.application.workflows.run_service import TERMINAL_STATUSES
from proseforge.application.writing.export_service import render_export
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.remaining import WorkflowRunModel
from proseforge.infrastructure.database.models.workflow_v2 import (
    WorkflowDefinitionModel,
    WorkflowNodeStateModel,
)
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

DEFAULT_LEASE_TTL_SECONDS = 60

EXPORT_FORMATS = {"txt", "md", "docx", "epub"}
EXPORT_TEMPLATES = {"web-serial", "submission", "archive"}


class QualityBlockedError(Exception):
    """Raised when a review node's quality gate blocks the chapter."""


@dataclass(frozen=True)
class NodeOutcome:
    used_tokens: int = 0
    used_cost: float = 0.0
    checkpoint: dict[str, object] = field(default_factory=dict)


NodeHandler = Callable[[SqlAlchemyUnitOfWork, WorkflowRunModel, WorkflowNodeStateModel, dict[str, object]], Awaitable[NodeOutcome]]


def _config(node_document: dict[str, object]) -> dict[str, object]:
    raw = node_document.get("config") or node_document.get("params") or {}
    return dict(raw) if isinstance(raw, dict) else {}


def topological_order(document: dict[str, object]) -> list[str]:
    """Kahn's algorithm, stable in definition order; definitions are validated
    acyclic at write time, this re-checks defensively before execution."""
    nodes = [str(node["id"]) for node in document["nodes"]]  # type: ignore[index]
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    indegree = dict.fromkeys(nodes, 0)
    for raw in document.get("edges", []):  # type: ignore[union-attr]
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            source, target = str(raw[0]), str(raw[1])
        else:
            source, target = str(raw["source"]), str(raw["target"])  # type: ignore[index]
        adjacency[source].append(target)
        indegree[target] += 1
    available = [node_id for node_id in nodes if indegree[node_id] == 0]
    order: list[str] = []
    while available:
        current = available.pop(0)
        order.append(current)
        for target in adjacency[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                available.append(target)
    if len(order) != len(nodes):
        raise ValueError("workflow graph must be acyclic")
    return order


async def _owner_id(uow: SqlAlchemyUnitOfWork, run: WorkflowRunModel) -> str:
    owner_id = await uow.session.scalar(select(ProjectModel.owner_id).where(ProjectModel.id == run.project_id))
    if owner_id is None:
        raise LookupError("workflow run project not found")
    return str(owner_id)


def _status_result(status: str) -> str:
    return "budget-blocked" if status == "BUDGET_BLOCKED" else status.lower()


def _as_utc(value: datetime) -> datetime:
    """SQLite 回读的 DateTime 是 naive；按 UTC 归一化后再比较。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def _intake_handler(uow: SqlAlchemyUnitOfWork, run: WorkflowRunModel, node: WorkflowNodeStateModel, config: dict[str, object]) -> NodeOutcome:
    del uow, run, node
    service = OutlineIntakeService()
    payload = config.get("outline")
    spec = service.parse(payload if isinstance(payload, dict) else config)
    questions = service.clarification_questions(spec)
    return NodeOutcome(checkpoint={
        "title": spec.title,
        "genre": spec.genre,
        "missing_required_fields": list(spec.missing_required_fields),
        "clarification_questions": list(questions),
        "confirmed": service.confirm(spec),
    })


async def _plan_handler(uow: SqlAlchemyUnitOfWork, run: WorkflowRunModel, node: WorkflowNodeStateModel, config: dict[str, object]) -> NodeOutcome:
    del uow, run, node
    plans = plan_chapters(
        volumes=int(config["volumes"]),
        chapters_per_volume=int(config["chapters_per_volume"]),
        word_target=int(config.get("word_target", 2000)),
        title_prefix=str(config.get("title_prefix", "Chapter")),
    )
    return NodeOutcome(checkpoint={
        "count": len(plans),
        "planned_chapters": [plan.chapter_no for plan in plans],
        "titles": [plan.title for plan in plans],
    })


async def _commit_chapter_content(uow: SqlAlchemyUnitOfWork, run: WorkflowRunModel, config: dict[str, object], *, rewritten: bool) -> NodeOutcome:
    chapter_id = str(config.get("chapter_id", ""))
    content = config.get("content")
    if not chapter_id or not isinstance(content, str):
        raise ValueError("write/rewrite nodes require chapter_id and content in config")
    chapter = await uow.chapters.get_owned(chapter_id, await _owner_id(uow, run))
    if chapter is None:
        raise LookupError("chapter not found")
    version = await uow.chapters.append_version(chapter_id=chapter_id, content=content)
    await uow.chapters.set_active_version(chapter_id, version.id)
    return NodeOutcome(checkpoint={
        "chapter_id": chapter_id,
        "version_id": version.id,
        "version_no": version.version_no,
        "rewritten": rewritten,
    })


async def _write_handler(uow: SqlAlchemyUnitOfWork, run: WorkflowRunModel, node: WorkflowNodeStateModel, config: dict[str, object]) -> NodeOutcome:
    del node
    return await _commit_chapter_content(uow, run, config, rewritten=False)


async def _rewrite_handler(uow: SqlAlchemyUnitOfWork, run: WorkflowRunModel, node: WorkflowNodeStateModel, config: dict[str, object]) -> NodeOutcome:
    del node
    return await _commit_chapter_content(uow, run, config, rewritten=True)


async def _review_handler(uow: SqlAlchemyUnitOfWork, run: WorkflowRunModel, node: WorkflowNodeStateModel, config: dict[str, object]) -> NodeOutcome:
    del node
    chapter_id = str(config.get("chapter_id", ""))
    if not chapter_id:
        raise ValueError("review nodes require chapter_id in config")
    owner_id = await _owner_id(uow, run)
    chapter = await uow.chapters.get_owned(chapter_id, owner_id)
    if chapter is None:
        raise LookupError("chapter not found")
    version = await uow.chapters.get_version_owned(chapter_id, str(chapter.active_version_id), owner_id) if chapter.active_version_id else None
    decision = RuleQualityService({}).run(version.content if version else "", chapter.chapter_no, {})
    checkpoint: dict[str, object] = {
        "chapter_id": chapter_id,
        "status": decision.status,
        "can_commit": decision.can_commit,
        "blocked_by": list(decision.blocked_by),
        "warnings": list(decision.warnings),
    }
    if version is not None:
        checkpoint["version_id"] = version.id
        checkpoint["content_hash"] = version.content_hash
    if decision.status == "BLOCK":
        raise QualityBlockedError("quality gate blocked the chapter")
    return NodeOutcome(checkpoint=checkpoint)


async def _export_handler(uow: SqlAlchemyUnitOfWork, run: WorkflowRunModel, node: WorkflowNodeStateModel, config: dict[str, object]) -> NodeOutcome:
    del node
    format_name = str(config.get("format", "txt"))
    template = str(config.get("template", "archive"))
    if format_name not in EXPORT_FORMATS:
        raise ValueError(f"unsupported export format: {format_name}")
    if template not in EXPORT_TEMPLATES:
        raise ValueError(f"unsupported export template: {template}")
    owner_id = await _owner_id(uow, run)
    project = await uow.session.get(ProjectModel, run.project_id)
    chapters = list(await uow.session.scalars(select(ChapterModel).where(ChapterModel.project_id == run.project_id).order_by(ChapterModel.chapter_no)))
    if not chapters:
        raise LookupError("no chapters to export")
    active_ids = [chapter.active_version_id for chapter in chapters if chapter.active_version_id]
    versions = {row.id: row for row in await uow.session.scalars(select(ChapterVersionModel).where(ChapterVersionModel.id.in_(active_ids)))} if active_ids else {}
    snapshot = [(chapter, versions[chapter.active_version_id]) for chapter in chapters if chapter.active_version_id in versions]
    if not snapshot:
        raise LookupError("no chapter has an active version to export")
    title = str(config.get("title") or (project.title if project else "ProseForge Export"))
    author = str(config.get("author", ""))
    locale = str(config.get("locale", "en"))
    artifact = render_export(format_name=format_name, chapters=[(chapter, version.content) for chapter, version in snapshot], title=title, author=author, locale=locale, template=template)
    manifest = await uow.exports.create(
        project_id=run.project_id,
        user_id=owner_id,
        format_name=format_name,
        template=template,
        title=title,
        author=author,
        locale=locale,
        version_ids=[version.id for _, version in snapshot],
        content_hashes={version.id: version.content_hash for _, version in snapshot},
        file_sha256=artifact.sha256,
        byte_size=len(artifact.body),
    )
    return NodeOutcome(checkpoint={
        "manifest_id": manifest.id,
        "file_sha256": artifact.sha256,
        "byte_size": len(artifact.body),
        "media_type": artifact.media_type,
    })


DEFAULT_HANDLERS: dict[str, NodeHandler] = {
    "intake": _intake_handler,
    "plan": _plan_handler,
    "write": _write_handler,
    "review": _review_handler,
    "rewrite": _rewrite_handler,
    "export": _export_handler,
}


class WorkflowRunExecutor:
    """Executes one v2 workflow run, one node per committed transaction."""

    def __init__(self, uow_factory: Callable[[], SqlAlchemyUnitOfWork], handlers: Mapping[str, NodeHandler] | None = None, *, lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS):
        self._uow_factory = uow_factory
        self._handlers = dict(DEFAULT_HANDLERS if handlers is None else handlers)
        self._lease_ttl = lease_ttl_seconds

    async def execute(self, run_id: str, lease_owner: str) -> str:
        planned = await self._prepare(run_id, lease_owner)
        if isinstance(planned, str):
            return planned
        order, documents = planned
        for node_key in order:
            result = await self._execute_node(run_id, node_key, documents[node_key], lease_owner)
            if result is not None:
                return result
        return await self._finalize(run_id, lease_owner)

    async def _prepare(self, run_id: str, lease_owner: str) -> str | tuple[list[str], dict[str, dict[str, object]]]:
        async with self._uow_factory() as uow:
            run = await uow.session.get(WorkflowRunModel, run_id)
            if run is None:
                return "run-not-found"
            if run.status in TERMINAL_STATUSES | {"PAUSED", "BUDGET_BLOCKED"}:
                return _status_result(run.status)
            if not await uow.workflows.acquire_lease(run, lease_owner, self._lease_ttl):
                return "lease-unavailable"
            if run.status == "QUEUED":
                run.status = "RUNNING"
            checkpoint = json.loads(run.checkpoint or "{}")
            definition = await uow.session.get(WorkflowDefinitionModel, str(checkpoint.get("definition_id", "")))
            if definition is None:
                run.status = "FAILED"
                run.last_error = "DefinitionMissing"
                run.lease_owner = None
                run.lease_expires_at = None
                await uow.workflows.append_event(run.id, "run.failed", {"status": "FAILED", "error": "DefinitionMissing"})
                await uow.commit()
                return "failed"
            document = json.loads(definition.definition_json)
            await uow.commit()
        documents = {str(node["id"]): node for node in document["nodes"]}
        return topological_order(document), documents

    async def _execute_node(self, run_id: str, node_key: str, node_document: dict[str, object], lease_owner: str) -> str | None:
        """Returns None when the node completed and the walk should continue."""
        now = datetime.now(UTC)
        async with self._uow_factory() as uow:
            run = await uow.session.get(WorkflowRunModel, run_id)
            if run is None:
                return "run-not-found"
            if run.status in {"PAUSED", "BUDGET_BLOCKED"} or run.status in TERMINAL_STATUSES:
                await self._release_run_lease(uow, run)
                await uow.commit()
                return _status_result(run.status)
            if run.lease_owner != lease_owner:
                return "lease-unavailable"
            await uow.workflows.heartbeat(run, lease_owner, self._lease_ttl)
            node = await uow.session.scalar(select(WorkflowNodeStateModel).where(WorkflowNodeStateModel.run_id == run_id, WorkflowNodeStateModel.node_key == node_key))
            if node is None:
                return await self._fail(uow, run, None, node_key, LookupError("node state missing"))
            if node.status == "COMPLETED":
                await uow.commit()
                return None
            if node.status == "BLOCKED":
                # 预算阻塞过的节点只能经控制面 retry（BLOCKED→PENDING）续跑；
                # 直接 resume 到此节点时重新落回 BUDGET_BLOCKED，绝不裸跑。
                run.status = "BUDGET_BLOCKED"
                await self._release_run_lease(uow, run)
                await uow.workflows.append_event(run.id, "run.budget_blocked", {"status": "BUDGET_BLOCKED", "node_key": node_key})
                await uow.commit()
                return "budget-blocked"
            if node.status == "RUNNING" and node.lease_owner not in (None, lease_owner) and node.lease_expires_at and _as_utc(node.lease_expires_at) > now:
                return "lease-unavailable"
            if node.status not in {"PENDING", "RUNNING"}:
                return await self._fail(uow, run, node, node_key, ValueError(f"node is {node.status}"))
            node.status = "RUNNING"
            node.lease_owner = lease_owner
            node.lease_expires_at = now + timedelta(seconds=self._lease_ttl)
            node.updated_at = now
            await uow.workflows.append_event(run.id, "node.started", {"node_key": node_key})
            config = _config(node_document)
            reserved_tokens = int(config.get("reserved_tokens", config.get("token_budget", node_document.get("reserved_tokens", 0))) or 0)
            reserved_cost = float(config.get("reserved_cost", config.get("cost_budget", node_document.get("reserved_cost", 0))) or 0)
            if not await uow.workflows.reserve_node_budget(run, node, reserved_tokens, reserved_cost):
                await self._release_run_lease(uow, run)
                await uow.commit()
                return "budget-blocked"
            kind = str(node_document.get("kind", node_document.get("type")))
            handler = self._handlers.get(kind)
            if handler is None:
                return await self._fail(uow, run, node, node_key, ValueError(f"unsupported node kind: {kind}"))
            try:
                outcome = await handler(uow, run, node, config)
            except Exception as exc:
                await uow.rollback()
                return await self._mark_failed(run_id, node_key, exc)
            finished = datetime.now(UTC)
            if run.lease_owner != lease_owner or node.lease_owner != lease_owner:
                await uow.rollback()
                return "lease-unavailable"
            node.used_tokens = outcome.used_tokens
            node.used_cost = outcome.used_cost
            node.reserved_tokens = 0  # 释放未用预留
            node.reserved_cost = 0
            node.status = "COMPLETED"
            node.checkpoint_json = json.dumps(outcome.checkpoint, ensure_ascii=False)
            node.lease_owner = None
            node.lease_expires_at = None
            node.updated_at = finished
            run.used_tokens = int(run.used_tokens or 0) + outcome.used_tokens
            run.estimated_cost = float(run.estimated_cost or 0) + outcome.used_cost
            await uow.workflows.append_event(run.id, "node.completed", {"node_key": node_key, "used_tokens": outcome.used_tokens, "used_cost": outcome.used_cost})
            await uow.commit()
            return None

    async def _fail(self, uow: SqlAlchemyUnitOfWork, run: WorkflowRunModel, node: WorkflowNodeStateModel | None, node_key: str, exc: Exception) -> str:
        now = datetime.now(UTC)
        if node is not None:
            node.status = "FAILED"
            node.lease_owner = None
            node.lease_expires_at = None
            node.reserved_tokens = 0
            node.reserved_cost = 0
            node.updated_at = now
        run.status = "FAILED"
        run.last_error = type(exc).__name__
        await self._release_run_lease(uow, run)
        await uow.workflows.append_event(run.id, "node.failed", {"node_key": node_key, "error": type(exc).__name__})
        await uow.workflows.append_event(run.id, "run.failed", {"status": "FAILED", "node_key": node_key, "error": type(exc).__name__})
        await uow.commit()
        return "failed"

    async def _mark_failed(self, run_id: str, node_key: str, exc: Exception) -> str:
        async with self._uow_factory() as uow:
            run = await uow.session.get(WorkflowRunModel, run_id)
            if run is None:
                return "run-not-found"
            node = await uow.session.scalar(select(WorkflowNodeStateModel).where(WorkflowNodeStateModel.run_id == run_id, WorkflowNodeStateModel.node_key == node_key))
            return await self._fail(uow, run, node, node_key, exc)

    async def _finalize(self, run_id: str, lease_owner: str) -> str:
        async with self._uow_factory() as uow:
            run = await uow.session.get(WorkflowRunModel, run_id)
            if run is None:
                return "run-not-found"
            if run.status in {"CANCELLED", "PAUSED", "BUDGET_BLOCKED"}:
                await self._release_run_lease(uow, run)
                await uow.commit()
                return _status_result(run.status)
            incomplete = await uow.session.scalar(select(WorkflowNodeStateModel.id).where(WorkflowNodeStateModel.run_id == run_id, WorkflowNodeStateModel.status != "COMPLETED").limit(1))
            if incomplete is not None:
                return await self._fail(uow, run, None, "", ValueError("workflow finished with incomplete nodes"))
            run.status = "COMPLETED"
            await self._release_run_lease(uow, run)
            await uow.workflows.append_event(run.id, "run.completed", {"status": "COMPLETED"})
            await uow.commit()
            return "completed"

    @staticmethod
    async def _release_run_lease(uow: SqlAlchemyUnitOfWork, run: WorkflowRunModel) -> None:
        del uow
        run.lease_owner = None
        run.lease_expires_at = None
