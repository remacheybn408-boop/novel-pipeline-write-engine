"""Batch chapter dispatcher (work-cluster mode).

Turns a completed analyze run (analyst artifact with a per-chapter
``chapters`` list) into a serial chain of single-chapter write runs:

- planning: ``on_run_terminal`` sees an analyze run COMPLETED, normalizes
  the analyst ``chapters`` (2+ entries, capped at BATCH_MAX_CHAPTERS) and
  appends a ``batch.planned`` event on the analyze run, then creates the
  chapter-1 write run in the SAME commit;
- advancing: every batch chapter run carries an idempotency key
  ``batch:{analyze_run_id}:{chapter_no}``; its terminal transition (any
  status) re-enters this module, which creates the next chapter's run.
  COMPLETED/FAILED/BUDGET_EXHAUSTED advance (a failed chapter is skipped,
  never drags the batch down); CANCELLED terminates the batch;
- state: the plan lives in ``batch.*`` events on the analyze run row plus
  the chapter runs' idempotency keys — no new table, survives worker
  restarts. ``create_agent_run``'s idempotency replay makes re-firing the
  hook (redelivery, sweeper replay) safe: a chapter run is never created
  or enqueued twice;
- guardrails: dispatch only while the project still exists and stays in
  work mode; when the last chapter settles, a ``batch.completed`` event
  records succeeded/skipped chapter numbers and the summary line is
  appended to the analyze run's swarm assistant message.

Everything here is hook-side bookkeeping: ``on_run_terminal`` never
raises, so a dispatch failure can never overturn run terminal state.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from sqlalchemy import select

from proseforge.application.agents.create_run import RunTaskSpec, create_agent_run
from proseforge.application.agents.intent import graph_for_intent, is_analyze_task_keys
from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.agents import (
    AgentArtifactModel,
    AgentEventModel,
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.models.conversation import MessageModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.events.hybrid import HybridEventStream
from proseforge.infrastructure.tasks.factory import create_task_queue

logger = logging.getLogger(__name__)

# Hard cap per batch: beyond this the plan is truncated (the batch.planned
# event carries truncated=True) and the user is told via the summary.
# 100 covers full-length novels (50 章连写是已验证的真实需求）；每章约
# 10 万 token，单批 100 章即约 1000 万 token 的模型消耗，再高无意义。
BATCH_MAX_CHAPTERS = 100
# Per-chapter write-run budget: same value as swarm_entry.DEFAULT_SWARM_BUDGET
# (12-task pipeline, ~100K/chapter measured + self-polish, final-gate revise
# rounds and the full-outline injection; ~180K average with 240K peaks, 300K
# worst-case headroom). Duplicated here to keep the hook module free of the
# swarm-entry import chain.
BATCH_CHAPTER_BUDGET = 300000

BATCH_KEY_PREFIX = "batch"
_EVENT_PLANNED = "batch.planned"
_EVENT_COMPLETED = "batch.completed"
_EVENT_TERMINATED = "batch.terminated"
_EVENT_STALLED = "batch.stalled"
# A batch that saw any of these never dispatches again (double-fire guard).
_TERMINAL_BATCH_EVENTS = (_EVENT_COMPLETED, _EVENT_TERMINATED, _EVENT_STALLED)

# 用户限量指令：「先写前5章」「写前10章」「第1章到第5章」「第1-5章」。
# 只认从第 1 章起的上限；范围起点 >1（如「第3到第5章」）不是限量，忽略。
# 负向断言 (?!\s*[:：]) 排除大纲卷目标题（「卷一（第 1-6 章：末世…）」）——
# 大纲正文里的章节范围不是用户的限量指令，误匹配会把整批截到卷一。
_LIMIT_FRONT_PATTERN = re.compile(r"前\s*([0-9]{1,3}|[一二三四五六七八九十]{1,3})\s*章(?!\s*[:：])")
_LIMIT_RANGE_PATTERN = re.compile(
    r"第\s*([0-9]{1,3}|[一二三四五六七八九十]{1,3})\s*章?\s*(?:到|至|[-—~～])\s*第?\s*([0-9]{1,3}|[一二三四五六七八九十]{1,3})\s*章(?!\s*[:：])"
)
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_number(text: str) -> int | None:
    """Arabic or simple Chinese numerals (十/二十/三十五) -> int."""
    if text.isdigit():
        return int(text)
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        units = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + units
    if len(text) == 1 and text in _CN_DIGITS:
        return _CN_DIGITS[text]
    return None


def requested_chapter_limit(goal: str) -> int | None:
    """User-requested chapter cap from the instruction tail of the goal.

    Returns the cap N for 「前N章」/「第1章到第N章」 style instructions,
    None when no limit is stated (batch covers the whole outline).
    """
    for match in _LIMIT_RANGE_PATTERN.finditer(goal or ""):
        start, end = _cn_number(match.group(1)), _cn_number(match.group(2))
        if start == 1 and end and end > 1:
            return end
    match = _LIMIT_FRONT_PATTERN.search(goal or "")
    if match:
        limit = _cn_number(match.group(1))
        if limit and limit > 0:
            return limit
    return None


def chapter_idempotency_key(analyze_run_id: str, chapter_no: int) -> str:
    """Deterministic per-chapter idempotency key (fits String(200))."""
    return f"{BATCH_KEY_PREFIX}:{analyze_run_id}:{chapter_no}"


def parse_batch_key(key: str | None) -> tuple[str, int] | None:
    """``batch:{analyze_run_id}:{chapter_no}`` -> (run id, chapter no)."""
    if not key:
        return None
    parts = key.split(":")
    if len(parts) != 3 or parts[0] != BATCH_KEY_PREFIX:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


def normalize_chapters(payload: dict[str, object]) -> list[dict[str, object]]:
    """Analyst payload -> ordered [{chapter_no, title, summary, hooks, target_words}].

    Non-dict entries are dropped; a missing/unparsable chapter_no falls
    back to the 1-based list position. Sorted by chapter_no so the serial
    chain walks the outline in order. ``target_words`` is carried through
    verbatim (int or string like "3000-5000字"); chapter_goal turns it
    into the explicit word-target line.
    """
    chapters = payload.get("chapters")
    if not isinstance(chapters, list):
        return []
    normalized: list[dict[str, object]] = []
    for index, entry in enumerate(chapters, start=1):
        if not isinstance(entry, dict):
            continue
        try:
            chapter_no = int(entry.get("chapter_no"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            chapter_no = index
        normalized.append({
            "chapter_no": chapter_no,
            "title": str(entry.get("title") or f"第{chapter_no}章"),
            "summary": str(entry.get("summary") or "").strip(),
            "hooks": str(entry.get("hooks") or "").strip(),
            "target_words": entry.get("target_words"),
        })
    normalized.sort(key=lambda chapter: int(chapter["chapter_no"]))
    return normalized


def normalize_volumes(payload: dict[str, object]) -> list[dict[str, object]]:
    """Analyst payload -> ordered [{volume_no, title, start, end}]（卷一等公民）。

    analyst 输出契约的可选 volumes 字段：{volume_no, title, chapter_range}，
    chapter_range 兼容 "1-10" / [1, 10] / {"start": 1, "end": 10} 三种形态。
    非法/缺字段条目丢弃；按 start 排序。返回空列表 = 无卷结构（下游回退
    goal 正则 / 固定 10 章一卷）。
    """
    volumes = payload.get("volumes")
    if not isinstance(volumes, list):
        return []
    normalized: list[dict[str, object]] = []
    for index, entry in enumerate(volumes, start=1):
        if not isinstance(entry, dict):
            continue
        start: int | None = None
        end: int | None = None
        chapter_range = entry.get("chapter_range")
        if isinstance(chapter_range, str):
            match = re.search(r"(\d{1,4})\s*(?:到|至|[-—~～])\s*(\d{1,4})", chapter_range)
            if match:
                start, end = int(match.group(1)), int(match.group(2))
        elif isinstance(chapter_range, (list, tuple)) and len(chapter_range) == 2:
            try:
                start, end = int(chapter_range[0]), int(chapter_range[1])
            except (TypeError, ValueError):
                start = end = None
        elif isinstance(chapter_range, dict):
            try:
                start, end = int(chapter_range.get("start")), int(chapter_range.get("end"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                start = end = None
        if start is None or end is None or start < 1 or end <= start:
            continue
        try:
            volume_no = int(entry.get("volume_no"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            volume_no = index
        normalized.append({"volume_no": volume_no, "title": str(entry.get("title") or ""), "start": start, "end": end})
    normalized.sort(key=lambda volume: int(volume["start"]))
    return normalized


def render_volume_labels(volumes: list[dict[str, object]]) -> str:
    """卷结构 → goal 文本标签行（rollup_recap._VOLUME_LABEL_PATTERN 可解析的
    格式「卷N（第 S-E 章：标题）」）；空卷列表返回空串。"""
    lines = []
    for volume in volumes:
        label = f"卷{volume['volume_no']}（第 {volume['start']}-{volume['end']} 章"
        title = str(volume.get("title") or "").strip()
        label += f"：{title}）" if title else "）"
        lines.append(label)
    return "\n".join(lines)


# Separator between the outline body and the trailing directive line in an
# analyze run's goal (「请严格按照以上大纲，一口气写完全部12章正文。」).
_GOAL_DIRECTIVE_SEPARATOR = "\n\n---\n\n"

_WORD_NUMBER_PATTERN = re.compile(r"(\d{3,5})")

# 「禁止明说」清单的确定性提取：后章 hooks 行里紧跟揭示关键词的 CJK 短语
# （2~12 字），以及后章 summary 的「真相是X」「真名X」「身份是X」
# 「X就是凶手/内鬼/叛徒」模式。宁缺毋滥：只认这些模式，提取不到就为空。
_HOOK_REVEAL_PATTERN = re.compile(r"(?:揭示|回收|真相|真名|身份)([一-鿿]{2,12})")
_SUMMARY_REVEAL_PATTERNS = (
    re.compile(r"真相是([一-鿿]{2,12})"),
    re.compile(r"身份是([一-鿿]{2,12})"),
    re.compile(r"真名([一-鿿]{2,12})"),
    re.compile(r"([一-鿿]{2,12})就是(?:凶手|内鬼|叛徒)"),
)


def later_chapter_reveals(chapters: list[dict[str, object]], chapter_no: int) -> list[str]:
    """后章专属揭示信息（谜底/真名/身份）清单，供本章 goal 的「禁止明说」行。
    只扫描 chapter_no 之后的章节；结果去重（已是更长条目子串的候选不再单列）。"""
    reveals: list[str] = []
    for chapter in chapters:
        if int(chapter["chapter_no"]) <= chapter_no:
            continue
        candidates = _HOOK_REVEAL_PATTERN.findall(str(chapter.get("hooks") or ""))
        for pattern in _SUMMARY_REVEAL_PATTERNS:
            candidates.extend(pattern.findall(str(chapter.get("summary") or "")))
        for candidate in candidates:
            if any(candidate in existing for existing in reveals):
                continue
            reveals.append(candidate)
    return reveals


def book_outline_from_goal(goal: str) -> str:
    """Full-book outline portion of an analyze run's goal: everything before
    the trailing ``---`` directive line. Goals without the separator are
    kept verbatim (the whole goal IS the outline)."""
    text = (goal or "").strip()
    if _GOAL_DIRECTIVE_SEPARATOR in text:
        return text.split(_GOAL_DIRECTIVE_SEPARATOR, 1)[0].strip()
    return text


def _min_target_words(value: object) -> int | None:
    """analyst 的 target_words（int 或 "3000-5000字"/"约3000字" 字符串）->
    字数下限；取字符串里的第一个数字（区间下限）。无法解析返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        words = int(value)
        return words if 100 <= words <= 100000 else None
    if isinstance(value, str):
        match = _WORD_NUMBER_PATTERN.search(value)
        if match:
            return int(match.group(1))
    return None


def chapter_goal(chapter: dict[str, object], *, book_outline: str = "", genre: str = "", forbidden: list[str] | None = None) -> str:
    """Write-run goal for one chapter. The leading 「写第N章」 guarantees
    writeback_chapter's parse_chapter_no resolves the chapter number; the
    outline fragment gives the pipeline this chapter's beats. The explicit
    「目标字数」 line is the word-count contract picked up by
    quality_gate.parse_min_words (highest priority) and by the
    scene_writer/rewrite hard-length requirement.

    ``genre``/``book_outline`` append a trailing global-context section so
    the writer and reviewer seats see the whole book's setting and
    foreshadowing. The appended section comes strictly after the chapter's
    own 「目标字数」/「伏笔/钩子」 lines, so quality_gate's first-match
    parsing still resolves this chapter's values even when the full-book
    outline carries its own such lines.

    ``forbidden`` (later_chapter_reveals' output) injects a 「禁止明说」
    line right before the book-outline section: later chapters' reveal
    info (谜底/真名/身份) must not be stated outright in this chapter."""
    lines = [f"写第{chapter['chapter_no']}章《{chapter['title']}》"]
    if chapter.get("summary"):
        lines.append(f"本章大纲：{chapter['summary']}")
    if chapter.get("hooks"):
        lines.append(f"伏笔/钩子：{chapter['hooks']}")
    min_words = _min_target_words(chapter.get("target_words"))
    if min_words is not None:
        lines.append(f"目标字数：不少于 {min_words} 字")
    if genre:
        lines.append(f"题材：{genre}")
    if forbidden:
        lines.append(f"禁止明说：{'、'.join(forbidden)}（属后续章节信息，本章只可侧面暗示，不可直接写出）")
    if book_outline:
        lines.append(f"全书大纲（仅作全局设定与伏笔参照，本章只写「写第{chapter['chapter_no']}章」指定的内容）：")
        lines.append(book_outline)
    return "\n".join(lines)


async def _add_batch_event(uow: SqlAlchemyUnitOfWork, run: AgentRunModel, event_type: str, data: dict[str, object]) -> None:
    """Row-locked sequence allocation, same discipline as the executor's
    add_event: (run_id, sequence) stays unique under hook replays."""
    locked = await uow.session.scalar(
        select(AgentRunModel).where(AgentRunModel.id == run.id).with_for_update().execution_options(populate_existing=True)
    )
    if locked is None:
        return
    sequence = int(locked.event_cursor) + 1
    uow.session.add(AgentEventModel(id=new_id(), run_id=locked.id, sequence=sequence, event_type=event_type, payload=json.dumps(data, ensure_ascii=False, sort_keys=True)))
    locked.event_cursor = sequence
    locked.updated_at = datetime.now(UTC)


async def _load_batch_plan(uow: SqlAlchemyUnitOfWork, analyze_run_id: str) -> tuple[dict[str, object] | None, bool]:
    """(latest batch.planned payload, batch-already-ended) for an analyze run."""
    planned: dict[str, object] | None = None
    ended = False
    rows = await uow.session.scalars(
        select(AgentEventModel)
        .where(AgentEventModel.run_id == analyze_run_id, AgentEventModel.event_type.in_((_EVENT_PLANNED, *_TERMINAL_BATCH_EVENTS)))
        .order_by(AgentEventModel.sequence)
    )
    for row in rows:
        if row.event_type == _EVENT_PLANNED:
            try:
                payload = json.loads(row.payload)
            except ValueError:
                continue
            if isinstance(payload, dict):
                planned = payload
        else:
            ended = True
    return planned, ended


async def _dispatch_chapter(uow: SqlAlchemyUnitOfWork, session_factory, settings, *, analyze_run: AgentRunModel, chapter: dict[str, object], forbidden: list[str] | None = None, volumes: list[dict[str, object]] | None = None) -> AgentRunModel:
    """Create + enqueue one chapter's write run inside the caller's uow
    (create_agent_run commits, so the batch event and the run land in one
    transaction). The batch idempotency key makes a hook replay return the
    existing run without a second enqueue. ``forbidden`` is later chapters'
    reveal info (later_chapter_reveals), injected into the goal as the
    「禁止明说」 line. ``volumes`` is the analyst's structured volume plan:
    rendered into goal-parseable labels so rollup/writeback resolve volume
    boundaries even when the user's outline carries no volume labels."""
    specs = [
        RunTaskSpec(id=str(item["id"]), role=str(item["role"]), depends_on=tuple(item.get("depends_on", ())))
        for item in graph_for_intent("write")
    ]
    queue = create_task_queue(settings, session_factory)
    # Global context for every chapter run: the full-book outline (analyze
    # goal minus its trailing directive line) plus the project genre, so
    # scene_writer/continuity_reviewer see cross-chapter settings instead
    # of only this chapter's slice. Both callers already hold the project
    # row in this session, so the get() below is an identity-map hit.
    project = await uow.session.get(ProjectModel, analyze_run.project_id)
    genre = str(project.genre) if project is not None and project.genre else ""
    book_outline = book_outline_from_goal(analyze_run.goal)
    volume_labels = render_volume_labels(volumes or [])
    if volume_labels:
        # 卷一等公民：analyst 结构化卷计划渲染为标签行附在全书大纲后，
        # writeback 的 volume_no 解析与 rollup 的卷边界都认这个格式。
        book_outline = (book_outline + "\n\n卷结构：\n" + volume_labels).strip()
    goal = chapter_goal(chapter, book_outline=book_outline, genre=genre, forbidden=forbidden)
    # Inherit the analyze run's model context: a normal-mode (single-model)
    # batch must stay on the user's selected model; a cluster batch keeps
    # single_model empty so create_agent_run's cluster resolution applies.
    chapter_run, _created = await create_agent_run(
        uow, queue,
        user_id=analyze_run.user_id, project_id=analyze_run.project_id,
        goal=goal,
        tasks=specs, budget_limit=BATCH_CHAPTER_BUDGET,
        master_key=settings.master_key, environment=settings.environment,
        idempotency_key=chapter_idempotency_key(analyze_run.id, int(chapter["chapter_no"])),
        provider=analyze_run.provider, model=analyze_run.model,
        force_single_model=bool(analyze_run.single_model),
    )
    return chapter_run


async def _plan_batch_after_analyze(session_factory, settings, *, run_id: str, user_id: str) -> bool:
    """Analyze run COMPLETED -> plan the batch + dispatch chapter 1.

    Returns True when this run was handled as an analyze run (whether or
    not a batch was planned); False for non-analyze runs and for outlines
    that stay on the manual per-chapter path (<2 usable chapters, broken
    artifact, project gone or no longer in work mode).
    """
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = await uow.session.get(AgentRunModel, run_id)
        if run is None or run.user_id != user_id:
            return False
        task_keys = list(await uow.session.scalars(select(AgentTaskModel.task_key).where(AgentTaskModel.run_id == run_id)))
        # analyze 图签名：现行四任务（三席位 + analyze_merge）与存量单任务图都认。
        if not is_analyze_task_keys(task_keys):
            return False
        planned, ended = await _load_batch_plan(uow, run_id)
        if planned is not None or ended:
            # Hook replay on an already-planned batch: the chain advances
            # from chapter-run terminals, never from re-planning.
            return True
        chapters: list[dict[str, object]] = []
        volumes: list[dict[str, object]] = []
        # 融合产出优先：analyze_merge 的 chapters 才是最终逐章工作流（结构
        # 席位的原始 chapters hooks 未补齐）；存量单 analyze 任务走兜底。
        task_key_by_id = {
            task_id: task_key
            for task_id, task_key in await uow.session.execute(select(AgentTaskModel.id, AgentTaskModel.task_key).where(AgentTaskModel.run_id == run_id))
        }
        merged_payload: dict[str, object] | None = None
        seat_payload: dict[str, object] | None = None
        for artifact in await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.run_id == run_id)):
            try:
                payload = json.loads(artifact.payload)
            except ValueError:
                continue
            if not isinstance(payload, dict) or not normalize_chapters(payload):
                continue
            if task_key_by_id.get(artifact.task_id) == "analyze_merge":
                merged_payload = payload
                break
            if seat_payload is None:
                seat_payload = payload
        chosen = merged_payload or seat_payload
        if chosen is not None:
            chapters = normalize_chapters(chosen)
            volumes = normalize_volumes(chosen)
        if len(chapters) < 2:
            # Unparseable/empty/single-chapter analyst output: keep the
            # existing "reply 写第 N 章" manual path, no error.
            return False
        project = await uow.session.get(ProjectModel, run.project_id)
        if project is None or project.mode != "work":
            return False
        truncated = len(chapters) > BATCH_MAX_CHAPTERS
        chapters = chapters[:BATCH_MAX_CHAPTERS]
        # User-stated limit (「先写前5章」) truncates further: without this the
        # batch ignores the instruction and writes the whole outline.
        limit = requested_chapter_limit(str(run.goal or ""))
        if limit is not None and len(chapters) > limit:
            chapters = [chapter for chapter in chapters if int(chapter["chapter_no"]) <= limit]
            truncated = True
        await _add_batch_event(uow, run, _EVENT_PLANNED, {"chapters": chapters, "total": len(chapters), "truncated": truncated, "volumes": volumes})
        try:
            await _dispatch_chapter(
                uow, session_factory, settings,
                analyze_run=run, chapter=chapters[0],
                forbidden=later_chapter_reveals(chapters, int(chapters[0]["chapter_no"])),
                volumes=volumes,
            )
        except Exception as exc:
            # Concurrency cap / queue down: record the stall (the planned
            # event is already flushed; commit it together) but never let
            # dispatch trouble overturn the analyze run's COMPLETED state.
            logger.warning("batch dispatch stalled at chapter 1 run_id=%s: %s", run_id, exc)
            await _add_batch_event(uow, run, _EVENT_STALLED, {"reason": type(exc).__name__, "chapter_no": int(chapters[0]["chapter_no"])})
            await uow.commit()
        return True


async def _batch_summary(uow: SqlAlchemyUnitOfWork, *, analyze_run_id: str, chapters: list[dict[str, object]]) -> dict[str, object]:
    """Succeeded/skipped chapter numbers from the batch's chapter runs,
    plus the Chinese summary line appended to the swarm message.

    A COMPLETED chapter run WITHOUT a chapter.written_back event is not
    a success: the writeback commit failed after run.completed, so the
    chapter body never landed. Count those as 写回异常 (the sweeper
    replays them; until then they stay out of the success list)."""
    numbers = [int(chapter["chapter_no"]) for chapter in chapters]
    status_by_no: dict[int, str] = {}
    run_id_by_no: dict[int, str] = {}
    rows = await uow.session.scalars(select(AgentRunModel).where(AgentRunModel.idempotency_key.like(f"{BATCH_KEY_PREFIX}:{analyze_run_id}:%")))
    chapter_run_ids: list[str] = []
    for chapter_run in rows:
        parsed = parse_batch_key(chapter_run.idempotency_key)
        if parsed is not None:
            status_by_no[parsed[1]] = chapter_run.status
            run_id_by_no[parsed[1]] = chapter_run.id
            chapter_run_ids.append(chapter_run.id)
    written_back_run_ids: set[str] = set()
    if chapter_run_ids:
        written_back_run_ids = set(await uow.session.scalars(
            select(AgentEventModel.run_id).where(AgentEventModel.run_id.in_(chapter_run_ids), AgentEventModel.event_type == "chapter.written_back")
        ))
    writeback_missing = [
        number for number in numbers
        if status_by_no.get(number) == "COMPLETED" and run_id_by_no.get(number) not in written_back_run_ids
    ]
    succeeded = [
        number for number in numbers
        if status_by_no.get(number) == "COMPLETED" and number not in writeback_missing
    ]
    skipped = [number for number in numbers if number in status_by_no and status_by_no[number] != "COMPLETED"]
    undispatched = [number for number in numbers if number not in status_by_no]
    parts = [f"总调度：批量写作完成，共 {len(chapters)} 章：成功 {len(succeeded)} 章"]
    if succeeded:
        parts.append(f"（第{'、'.join(str(number) for number in succeeded)}章）")
    if skipped:
        parts.append(f"，跳过 {len(skipped)} 章（第{'、'.join(str(number) for number in skipped)}章，可在 run 详情页重试）")
    if writeback_missing:
        parts.append(f"，写回异常 {len(writeback_missing)} 章（第{'、'.join(str(number) for number in writeback_missing)}章，正文未落库，系统将自动补写或可在 run 详情页重试）")
    if undispatched:
        parts.append(f"，{len(undispatched)} 章未派发")
    text = "".join(parts) + "。"
    return {"total": len(chapters), "succeeded": succeeded, "skipped": skipped, "writeback_missing": writeback_missing, "undispatched": undispatched, "text": text}


async def _advance_batch_after_chapter(session_factory, settings, *, run_id: str, user_id: str, status: str) -> None:
    """Batch chapter run reached a terminal status: dispatch the next
    chapter, terminate on cancel, or close the batch with a summary."""
    message_id: str | None = None
    conversation_id: str | None = None
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = await uow.session.get(AgentRunModel, run_id)
        if run is None or run.user_id != user_id:
            return
        parsed = parse_batch_key(run.idempotency_key)
        if parsed is None:
            return  # not a batch chapter run
        analyze_run_id, done_chapter_no = parsed
        analyze_run = await uow.session.get(AgentRunModel, analyze_run_id)
        if analyze_run is None:
            return  # project/analyze run deleted (cascade): chain dies with it
        plan, ended = await _load_batch_plan(uow, analyze_run_id)
        if plan is None or ended:
            return
        chapters = [chapter for chapter in plan.get("chapters") or [] if isinstance(chapter, dict)]  # type: ignore[union-attr]
        numbers = [int(chapter["chapter_no"]) for chapter in chapters]
        if done_chapter_no not in numbers:
            return  # stale key from an older plan: never advance off-plan
        if status == "CANCELLED":
            # User cancelled a chapter mid-batch: stop the chain cleanly.
            await _add_batch_event(uow, analyze_run, _EVENT_TERMINATED, {"reason": "chapter run cancelled", "chapter_no": done_chapter_no})
            await uow.commit()
            return
        next_chapter = next((chapter for chapter in chapters if int(chapter["chapter_no"]) > done_chapter_no), None)
        if next_chapter is not None:
            project = await uow.session.get(ProjectModel, analyze_run.project_id)
            if project is None or project.mode != "work":
                await _add_batch_event(uow, analyze_run, _EVENT_TERMINATED, {"reason": "project deleted or left work mode"})
                await uow.commit()
                return
            try:
                await _dispatch_chapter(
                    uow, session_factory, settings,
                    analyze_run=analyze_run, chapter=next_chapter,
                    forbidden=later_chapter_reveals(chapters, int(next_chapter["chapter_no"])),
                    volumes=[volume for volume in plan.get("volumes") or [] if isinstance(volume, dict)],  # type: ignore[union-attr]
                )
            except Exception as exc:
                # Concurrency cap / queue down: stall the batch (auditable),
                # do not fail the chapter run that just finished.
                logger.warning("batch dispatch stalled run_id=%s chapter=%s: %s", analyze_run_id, next_chapter["chapter_no"], exc)
                await _add_batch_event(uow, analyze_run, _EVENT_STALLED, {"reason": type(exc).__name__, "chapter_no": int(next_chapter["chapter_no"])})
                await uow.commit()
            return
        # Last chapter settled (FAILED/BUDGET_EXHAUSTED chapters were
        # skipped along the way): close the batch and tell the user.
        summary = await _batch_summary(uow, analyze_run_id=analyze_run_id, chapters=chapters)
        await _add_batch_event(uow, analyze_run, _EVENT_COMPLETED, summary)
        message = await uow.session.scalar(select(MessageModel).where(MessageModel.agent_run_id == analyze_run_id))
        if message is not None and message.status == "COMPLETED":
            message.content = (message.content or "") + "\n\n" + str(summary["text"])
            conversation_id = await uow.conversations.conversation_id_for_message(message.id)
            message_id = message.id
        await uow.commit()
    if message_id is not None:
        # Same event shape the run writeback publishes, so the ChatPage
        # subscription refreshes the appended summary line.
        event_stream = HybridEventStream(session_factory, settings.redis_url)
        event: dict[str, object] = {"event": "message.completed", "message_id": message_id, "status": "COMPLETED"}
        await event_stream.publish(f"message:{message_id}", event)
        if conversation_id:
            await event_stream.publish(f"conversation:{conversation_id}", event)


async def on_run_terminal(session_factory, settings, *, run_id: str, user_id: str, status: str) -> None:
    """Batch-dispatch hook fired on every run terminal transition
    (executor exits + message-sweeper replay). Never raises: a hook
    failure must not break terminal bookkeeping."""
    try:
        if status == "COMPLETED" and await _plan_batch_after_analyze(session_factory, settings, run_id=run_id, user_id=user_id):
            return
        await _advance_batch_after_chapter(session_factory, settings, run_id=run_id, user_id=user_id, status=status)
    except Exception:
        logger.exception("batch dispatch hook failed run_id=%s status=%s", run_id, status)
