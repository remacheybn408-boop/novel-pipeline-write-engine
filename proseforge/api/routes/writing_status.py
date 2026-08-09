"""Writing-progress aggregation for the work-mode chat panel.

GET /api/v1/projects/{id}/writing-status folds three existing data sources
(agent_runs / agent_tasks / chapters, plus the analyze run's batch plan) into
a per-chapter status list — no new tables:

- chapter universe: the union of the latest batch plan's chapter list
  (``batch.planned`` event on the newest analyze run, falling back to the
  analyst artifact) and the chapters table;
- per-chapter status: derived from the chapter's latest write run (a run
  whose task graph carries ``scene_*`` drafting nodes). The chapter number
  comes from run.chapter_id, the batch idempotency key
  (``batch:{analyze_run_id}:{chapter_no}``), or the goal's 「写第 N 章」
  line — the same resolutions the writeback path uses;
- stage mapping: a running ``scene_*`` task means drafting (writing),
  ``review_*``/``merge`` means reviewing, ``rewrite``/``recheck`` means
  rewriting; terminal run states map to completed (active version written
  back) / failed; no run at all means not_started;
- auto_pause: the newest PAUSED run's latest ``run.auto_paused`` event
  (provider/model/error/streak), null for manual pauses and healthy runs.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from proseforge.api.dependencies import current_user, require_work_project, unit_of_work
from proseforge.application.agents.batch_dispatch import (
    normalize_chapters,
    parse_batch_key,
)
from proseforge.application.agents.intent import is_analyze_task_keys
from proseforge.application.agents.review_target import parse_chapter_no
from proseforge.application.auth.service import AuthUser
from proseforge.infrastructure.database.models.agents import (
    AgentArtifactModel,
    AgentEventModel,
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.models.chapter import ChapterModel
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1", tags=["writing-status"])

ChapterStatus = Literal["not_started", "writing", "reviewing", "rewriting", "completed", "failed"]

# 奥莉维亚·动态承诺的三个流水线节点（task_key -> lanes 字段名）。
_PROMISE_NODE_KEYS = (("promise_contract", "contract"), ("promise_verify", "verify"), ("promise_register", "register"))

# Display order of the write pipeline (intent.graph_for_intent("write")); the
# current stage is the RUNNING task furthest along this order. review_council
# sits between the review battery and merge（评审合议，约翰逊协作化）.
_PIPELINE_ORDER = (
    "planner", "character", "scene_a", "scene_b", "scene_c", "scene_d", "select",
    "review_continuity", "review_adversarial", "review_style", "review_council", "merge",
    "rewrite", "recheck",
)


def _pipeline_rank(task_key: str) -> int:
    if task_key in _PIPELINE_ORDER:
        return _PIPELINE_ORDER.index(task_key)
    if task_key.startswith("scene"):
        return _PIPELINE_ORDER.index("scene_d")
    if task_key.startswith("review"):
        return _PIPELINE_ORDER.index("review_council")
    if task_key.startswith("analyze"):
        return _PIPELINE_ORDER.index("planner")
    return -1


def _stage_for_task(task_key: str) -> tuple[ChapterStatus, str]:
    """RUNNING task_key -> (chapter status, 当前环节描述)."""
    if task_key.startswith("scene"):
        return "writing", "场景起草中"
    if task_key == "review_council":
        return "reviewing", "评审合议中"
    if task_key.startswith("review"):
        return "reviewing", "审校中"
    if task_key == "merge":
        return "reviewing", "意见合并中"
    if task_key == "rewrite":
        return "rewriting", "改写中"
    if task_key == "recheck":
        return "rewriting", "复核中"
    if task_key == "planner":
        return "writing", "写作规划中"
    if task_key == "character":
        return "writing", "角色设计中"
    if task_key == "select":
        return "writing", "择优定稿中"
    if task_key in ("analyze_structure", "analyze_cast", "analyze_hooks"):
        return "writing", "大纲拆解中"
    if task_key == "analyze_merge":
        return "writing", "大纲融合中"
    return "writing", "写作中"


def derive_chapter_status(
    run_status: str | None,
    running_task_keys: list[str],
    *,
    has_active_version: bool,
) -> tuple[ChapterStatus, str]:
    """One chapter's (status, stage) from its latest write run.

    Pure function kept separate from the query code so the state machine is
    unit-testable without a database.
    """
    if run_status is None:
        return "not_started", "未开始"
    if run_status in ("FAILED", "BUDGET_EXHAUSTED"):
        return "failed", "写作失败"
    if run_status == "CANCELLED":
        return "failed", "已取消"
    if run_status == "COMPLETED":
        # Writeback normally lands before the run completes; a COMPLETED run
        # without an active version is still mid-delivery, not done.
        return ("completed", "已完成") if has_active_version else ("writing", "定稿回写中")
    if run_status == "PENDING":
        return "writing", "排队中"
    if run_status == "PAUSED":
        return "writing", "已暂停"
    # RUNNING: the stage comes from the RUNNING task furthest down the
    # pipeline; between tasks (lease handoff) report a generic 写作中.
    if running_task_keys:
        current = max(running_task_keys, key=_pipeline_rank)
        return _stage_for_task(current)
    return "writing", "写作中"


async def _batch_plan_chapters(
    uow: SqlAlchemyUnitOfWork,
    analyze_run_ids: list[str],
) -> list[dict[str, object]]:
    """Latest batch plan's chapter list: ``batch.planned`` event payloads,
    falling back to the newest analyze run's analyst artifact."""
    if not analyze_run_ids:
        return []
    planned_by_run: dict[str, dict[str, object]] = {}
    rows = await uow.session.scalars(
        select(AgentEventModel)
        .where(AgentEventModel.run_id.in_(analyze_run_ids), AgentEventModel.event_type == "batch.planned")
        .order_by(AgentEventModel.sequence)
    )
    for row in rows:
        try:
            payload = json.loads(row.payload)
        except ValueError:
            continue
        if isinstance(payload, dict):
            planned_by_run[row.run_id] = payload
    for run_id in reversed(analyze_run_ids):
        planned = planned_by_run.get(run_id)
        if planned is not None:
            chapters = planned.get("chapters")
            if isinstance(chapters, list):
                return [chapter for chapter in chapters if isinstance(chapter, dict)]
    # No planned event (batch stalled or manual path): read the newest
    # analyze run's artifact directly. The analyze_merge fusion output is
    # the final workflow — a seat artifact (structure chapters without the
    # hooks seat's enrichment) is only a fallback.
    analyze_run_id = analyze_run_ids[-1]
    task_key_by_id = {
        task_id: task_key
        for task_id, task_key in await uow.session.execute(select(AgentTaskModel.id, AgentTaskModel.task_key).where(AgentTaskModel.run_id == analyze_run_id))
    }
    merged_chapters: list[dict[str, object]] = []
    seat_chapters: list[dict[str, object]] = []
    for artifact in await uow.session.scalars(
        select(AgentArtifactModel).where(AgentArtifactModel.run_id == analyze_run_id)
    ):
        try:
            payload = json.loads(artifact.payload)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        chapters = normalize_chapters(payload)
        if not chapters:
            continue
        if task_key_by_id.get(artifact.task_id) == "analyze_merge":
            merged_chapters = chapters
            break
        if not seat_chapters:
            seat_chapters = chapters
    return merged_chapters or seat_chapters


def _lane_for(entries: list[dict[str, object]], status: ChapterStatus) -> dict[str, object]:
    """One pipeline lane's brief: lowest-numbered chapter currently in the
    given status, else idle."""
    current = next((entry for entry in entries if entry["status"] == status), None)
    if current is None:
        return {"active": False, "chapter_no": None, "detail": "空闲"}
    return {"active": True, "chapter_no": current["chapter_no"], "detail": current["stage"]}


def _promise_pipeline_lane(
    latest_run_by_chapter: dict[int, AgentRunModel],
    tasks_by_run: dict[str, list[AgentTaskModel]],
    entries: list[dict[str, object]],
) -> dict[str, object]:
    """奥莉维亚三节点状态：优先取在写章节的 run，否则最近一次 write run。"""
    in_flight = next(
        (entry for entry in entries if entry["status"] in ("writing", "reviewing", "rewriting")),
        None,
    )
    run: AgentRunModel | None = None
    chapter_no: int | None = None
    if in_flight is not None:
        chapter_no = int(in_flight["chapter_no"])
        run = latest_run_by_chapter.get(chapter_no)
    if run is None and latest_run_by_chapter:
        chapter_no = max(latest_run_by_chapter)
        # latest write run = the one created last, not the highest chapter
        run = max(latest_run_by_chapter.values(), key=lambda candidate: (candidate.created_at, candidate.id))
        chapter_no = next((no for no, candidate in latest_run_by_chapter.items() if candidate.id == run.id), chapter_no)
    lane: dict[str, object] = {"active": False, "chapter_no": chapter_no, "contract": None, "verify": None, "register": None}
    if run is None:
        return lane
    status_by_key = {task.task_key: task.status for task in tasks_by_run.get(run.id, [])}
    for task_key, field in _PROMISE_NODE_KEYS:
        lane[field] = status_by_key.get(task_key)
    lane["active"] = any(status_by_key.get(task_key) in ("PENDING", "RUNNING") for task_key, _field in _PROMISE_NODE_KEYS)
    return lane


@router.get("/projects/{project_id}/writing-status")
async def writing_status(
    project_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        chapters = list(
            await uow.session.scalars(select(ChapterModel).where(ChapterModel.project_id == project_id))
        )
        chapter_no_by_id = {chapter.id: chapter.chapter_no for chapter in chapters}
        chapter_by_no = {chapter.chapter_no: chapter for chapter in chapters}
        runs = list(
            await uow.session.scalars(
                select(AgentRunModel)
                .where(AgentRunModel.project_id == project_id, AgentRunModel.user_id == user.id)
                .order_by(AgentRunModel.created_at, AgentRunModel.id)
            )
        )
        tasks_by_run: dict[str, list[AgentTaskModel]] = {run.id: [] for run in runs}
        if runs:
            for task in await uow.session.scalars(
                select(AgentTaskModel).where(AgentTaskModel.run_id.in_([run.id for run in runs]))
            ):
                tasks_by_run.setdefault(task.run_id, []).append(task)

        # Latest write run per chapter (runs are created_at-ordered, so a
        # later assignment wins). A write run carries scene_* drafting tasks.
        latest_run_by_chapter: dict[int, AgentRunModel] = {}
        analyze_run_ids: list[str] = []
        for run in runs:
            task_keys = [task.task_key for task in tasks_by_run.get(run.id, [])]
            if is_analyze_task_keys(task_keys):
                analyze_run_ids.append(run.id)
                continue
            if not any(key.startswith("scene") for key in task_keys):
                continue
            chapter_no: int | None = None
            if run.chapter_id and run.chapter_id in chapter_no_by_id:
                chapter_no = chapter_no_by_id[run.chapter_id]
            else:
                parsed = parse_batch_key(run.idempotency_key)
                if parsed is not None:
                    chapter_no = parsed[1]
                else:
                    chapter_no = parse_chapter_no(run.goal or "")
            if chapter_no is not None:
                latest_run_by_chapter[chapter_no] = run

        plan_chapters = await _batch_plan_chapters(uow, analyze_run_ids)
        plan_title_by_no = {
            int(chapter["chapter_no"]): str(chapter.get("title") or "")
            for chapter in plan_chapters
            if isinstance(chapter.get("chapter_no"), int)
        }
        chapter_numbers = sorted(set(plan_title_by_no) | set(chapter_by_no))

        entries: list[dict[str, object]] = []
        current_chapter_no: int | None = None
        for chapter_no in chapter_numbers:
            chapter = chapter_by_no.get(chapter_no)
            run = latest_run_by_chapter.get(chapter_no)
            running_keys = [
                task.task_key for task in tasks_by_run.get(run.id, []) if task.status == "RUNNING"
            ] if run is not None else []
            status_value, stage = derive_chapter_status(
                run.status if run is not None else None,
                running_keys,
                has_active_version=bool(chapter and chapter.active_version_id),
            )
            if status_value in ("writing", "reviewing", "rewriting") and (
                current_chapter_no is None or chapter_no < current_chapter_no
            ):
                current_chapter_no = chapter_no
            entries.append({
                "chapter_no": chapter_no,
                "title": (chapter.title if chapter is not None else "") or plan_title_by_no.get(chapter_no, ""),
                "chapter_id": chapter.id if chapter is not None else None,
                "status": status_value,
                "stage": stage,
                "downloadable": status_value == "completed",
            })
        # 承诺台账：kind='promise' 的 story_bible_entries 按状态计数（一次聚合）。
        ledger_rows = await uow.session.execute(
            select(StoryBibleEntryModel.status, func.count())
            .where(StoryBibleEntryModel.project_id == project_id, StoryBibleEntryModel.kind == "promise")
            .group_by(StoryBibleEntryModel.status)
        )
        ledger = {"open": 0, "developing": 0, "resolved": 0}
        for status_value, count in ledger_rows:
            if status_value in ledger:
                ledger[status_value] = int(count)
        # Auto-pause passthrough: the project's newest PAUSED run + its
        # latest run.auto_paused event payload. A manual pause carries no
        # such event -> None, and the panel shows no resume banner.
        auto_pause: dict[str, object] | None = None
        paused_run = next((run for run in reversed(runs) if run.status == "PAUSED"), None)
        if paused_run is not None:
            auto_pause_row = await uow.session.scalar(
                select(AgentEventModel)
                .where(AgentEventModel.run_id == paused_run.id, AgentEventModel.event_type == "run.auto_paused")
                .order_by(AgentEventModel.sequence.desc())
                .limit(1)
            )
            if auto_pause_row is not None:
                try:
                    auto_pause_payload = json.loads(auto_pause_row.payload)
                except ValueError:
                    auto_pause_payload = {}
                auto_pause = {
                    "run_id": paused_run.id,
                    "reason": str(auto_pause_payload.get("error") or ""),
                    "provider": str(auto_pause_payload.get("provider") or ""),
                    "model": str(auto_pause_payload.get("model") or ""),
                    "streak": int(auto_pause_payload.get("streak") or 0),
                }
        return {
            "project_id": project_id,
            "total_chapters": len(entries),
            "current_chapter_no": current_chapter_no,
            "chapters": entries,
            "auto_pause": auto_pause,
            "lanes": {
                "writing": _lane_for(entries, "writing"),
                "reviewing": _lane_for(entries, "reviewing"),
                "rewriting": _lane_for(entries, "rewriting"),
                "promise_pipeline": _promise_pipeline_lane(latest_run_by_chapter, tasks_by_run, entries),
                "promise_ledger": ledger,
            },
        }
