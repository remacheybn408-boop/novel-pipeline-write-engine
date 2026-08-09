"""Hierarchical recap rollup producer (memory pyramid, phase-2 item 7).

Trigger: ``summarize_chapter`` enqueues a ``rollup_recap`` retrieval job
when a chapter summary lands on a volume's last chapter. This worker then:

- volume recap: compresses the volume's chapter-version summaries into a
  <=1500-token recap that must cover mainline progress, character state
  changes, open hooks and key items/locations;
- book recap: rolls the OLD book recap plus the NEW volume recap into a
  refreshed <=1000-token book recap (incremental, never a full recompute);
- era recap: every 10 volumes, compresses that decade's volume recaps one
  level higher.

Volume boundaries come from the batch plan first: the analyze run's goal
carries outline volume labels (「卷一（第 1-6 章：…）」) which are parsed
into chapter ranges; without labels the fallback is fixed 10-chapter
volumes. The unique (project_id, level, span_start) constraint makes
regeneration an idempotent upsert. LLM failures ride the same pending ->
failed retry chain as the summarizer — an empty model output is treated
as an error and NEVER written.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from sqlalchemy import select

from proseforge.domain.common.ids import new_id
from proseforge.domain.ports.model_provider import GenerationRequest

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
VOLUME_FALLBACK_CHAPTERS = 10
VOLUMES_PER_ERA = 10
VOLUME_TOKEN_BUDGET = 1500
BOOK_TOKEN_BUDGET = 1000
BOOK_SPAN_START = 1

# char/2 proxy for CJK-heavy text, same convention as retrieval indexing.
_CHARS_PER_TOKEN = 2

_SYSTEM_PROMPT = "你是小说编辑助手。只输出梗概正文，不要输出任何解释、标题或 Markdown 标记。"

_VOLUME_PROMPT_TEMPLATE = """以下是小说第{span_start}-{span_end}章（同一卷）各章的浓缩摘要。把它们压缩成一段不超过{token_budget} token 的卷梗概，必须显式覆盖四点：主线进展、人物状态变化、未结伏笔、关键道具与地点。

各章摘要：
{summaries}"""

_BOOK_PROMPT_TEMPLATE = """以下是小说全书的旧梗概和刚完成一卷（第{span_start}-{span_end}章）的新卷梗概。把二者滚动融合成一段不超过{token_budget} token 的新全书梗概：以旧梗概为底，并入新卷梗概的增量信息，必须保留主线进展、人物状态变化、未结伏笔、关键道具与地点。

旧全书梗概：
{old_book}

新卷梗概：
{new_volume}"""

_BOOK_SEED_PROMPT_TEMPLATE = """以下是小说第一卷（第{span_start}-{span_end}章）的卷梗概。以它为底，写一段不超过{token_budget} token 的全书梗概，必须保留主线进展、人物状态变化、未结伏笔、关键道具与地点。

卷梗概：
{new_volume}"""

_ERA_PROMPT_TEMPLATE = """以下是小说连续 {count} 卷（第{span_start}-{span_end}章）的各卷梗概。把它们压缩成一段部/纪元级梗概，必须显式覆盖：主线进展、人物状态变化、未结伏笔、关键道具与地点。

各卷梗概：
{volumes}"""

# Outline volume labels: 「卷一（第 1-6 章：…）」「卷2（第7到12章…）」.
# Same family of patterns batch_dispatch's chapter-limit parser excludes.
_VOLUME_LABEL_PATTERN = re.compile(
    r"卷\s*[0-9一二三四五六七八九十百零两]{1,4}\s*[（(]\s*第\s*([0-9]{1,4})\s*(?:到|至|[-—~～])\s*第?\s*([0-9]{1,4})\s*章"
)


def parse_volume_spans(text: str) -> list[tuple[int, int]]:
    """Outline text -> sorted, non-overlapping (start, end) chapter ranges
    from volume labels. Ranges with end <= start or overlapping an
    already-kept range are dropped (defensive: labels are model output)."""
    spans: list[tuple[int, int]] = []
    for match in _VOLUME_LABEL_PATTERN.finditer(text or ""):
        start, end = int(match.group(1)), int(match.group(2))
        if start < 1 or end <= start:
            continue
        if any(start <= existing_end and end >= existing_start for existing_start, existing_end in spans):
            continue
        spans.append((start, end))
    spans.sort()
    return spans


def fallback_volume_span(chapter_no: int) -> tuple[int, int]:
    """Fixed 10-chapter volume containing chapter_no (1-10, 11-20, ...)."""
    start = ((chapter_no - 1) // VOLUME_FALLBACK_CHAPTERS) * VOLUME_FALLBACK_CHAPTERS + 1
    return start, start + VOLUME_FALLBACK_CHAPTERS - 1


def resolve_volume_span(chapter_no: int, spans: list[tuple[int, int]]) -> tuple[int, int]:
    """The volume span containing chapter_no: labeled span when the outline
    labels cover it, fixed 10-chapter fallback otherwise."""
    for start, end in spans:
        if start <= chapter_no <= end:
            return start, end
    return fallback_volume_span(chapter_no)


def volume_end_span(chapter_no: int, spans: list[tuple[int, int]]) -> tuple[int, int] | None:
    """(start, end) when chapter_no closes its volume, None otherwise."""
    start, end = resolve_volume_span(chapter_no, spans)
    return (start, end) if chapter_no == end else None


def volume_index(span: tuple[int, int], spans: list[tuple[int, int]]) -> int:
    """1-based volume ordinal: position inside the labeled spans, or the
    fixed-10 ordinal under the fallback."""
    for index, labeled in enumerate(sorted(spans), start=1):
        if labeled == span:
            return index
    return (span[0] - 1) // VOLUME_FALLBACK_CHAPTERS + 1


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def clip_to_budget(text: str, token_budget: int) -> str:
    """Hard cap on the model output: prompt asks for the budget, this
    truncates the overflow so a chatty model can never overshoot."""
    max_chars = token_budget * _CHARS_PER_TOKEN
    return text if len(text) <= max_chars else text[:max_chars].rstrip()


async def load_volume_spans(uow, project_id: str) -> list[tuple[int, int]]:
    """Volume boundaries from the project's batch plan: the analyze run
    that planned the batch carries the full outline (with volume labels)
    in its goal. Newest planned batch wins; no labels -> empty list and
    the caller falls back to fixed 10-chapter volumes."""
    from proseforge.infrastructure.database.models.agents import (
        AgentEventModel,
        AgentRunModel,
    )

    rows = await uow.session.scalars(
        select(AgentRunModel.goal)
        .join(AgentEventModel, AgentEventModel.run_id == AgentRunModel.id)
        .where(AgentRunModel.project_id == project_id, AgentEventModel.event_type == "batch.planned")
        .order_by(AgentEventModel.sequence.desc())
    )
    for goal in rows:
        spans = parse_volume_spans(goal or "")
        if spans:
            return spans
    return []


async def maybe_enqueue_rollup(uow, *, project_id: str, chapter_id: str, chapter_no: int) -> str | None:
    """Enqueue a rollup_recap job when chapter_no closes its volume.
    Called by summarize_chapter right after the summary commit; returns
    the new job id (None when the chapter is mid-volume). The caller's
    uow must commit for the job row to land."""
    spans = await load_volume_spans(uow, project_id)
    if volume_end_span(chapter_no, spans) is None:
        return None
    job = await uow.retrieval.enqueue_job(
        project_id=project_id, job_type="rollup_recap", source_type="chapter", source_id=chapter_id
    )
    return str(job.id)


async def enqueue_stale_recap_recompute(uow, *, project_id: str, chapter_no: int, user_id: str) -> list[str]:
    """Lazy recompute (phase-2 item 8): called from the scene-pack build
    right before the next chapter is written. Every STALE volume recap
    already completed (span_end < chapter_no) gets a rollup_recap job
    keyed to the volume's last chapter — the normal production pipeline
    then recomputes volume+book+era and clears the stale flags. A volume
    with a pending/running rollup job is not re-enqueued (chat-mode scene
    packs would otherwise flood the queue). One recap.recompute_queued
    audit event per new job. Returns the new job ids; the caller commits
    and dispatches (dispatch failure is safe: the job_type-routed sweeper
    picks stranded rows up)."""
    from proseforge.infrastructure.database.models.chapter import ChapterModel
    from proseforge.infrastructure.database.models.recap import RecapRollupModel
    from proseforge.infrastructure.database.models.remaining import AuditLogModel
    from proseforge.infrastructure.database.models.retrieval import RetrievalJobModel

    stale_volumes = list((await uow.session.scalars(
        select(RecapRollupModel).where(
            RecapRollupModel.project_id == project_id,
            RecapRollupModel.level == "volume",
            RecapRollupModel.stale.is_(True),
            RecapRollupModel.span_end < chapter_no,
        ).order_by(RecapRollupModel.span_start)
    )).all())
    job_ids: list[str] = []
    for rollup in stale_volumes:
        end_chapter = await uow.session.scalar(
            select(ChapterModel).where(
                ChapterModel.project_id == project_id,
                ChapterModel.chapter_no == rollup.span_end,
            )
        )
        if end_chapter is None:
            continue
        in_flight = await uow.session.scalar(
            select(RetrievalJobModel.id).where(
                RetrievalJobModel.job_type == "rollup_recap",
                RetrievalJobModel.source_type == "chapter",
                RetrievalJobModel.source_id == end_chapter.id,
                RetrievalJobModel.status.in_(["pending", "running"]),
            )
        )
        if in_flight is not None:
            continue
        job = await uow.retrieval.enqueue_job(
            project_id=project_id, job_type="rollup_recap", source_type="chapter", source_id=end_chapter.id
        )
        job_ids.append(str(job.id))
        uow.session.add(AuditLogModel(
            id=new_id(), user_id=user_id, action="recap.recompute_queued",
            target_type="recap_rollup", target_id=rollup.id,
            payload=json.dumps({
                "project_id": project_id, "level": rollup.level,
                "span_start": rollup.span_start, "span_end": rollup.span_end,
                "trigger_chapter": chapter_no, "job_id": str(job.id),
            }, ensure_ascii=False, separators=(",", ":")),
        ))
    return job_ids


async def run_rollup_job(payload: dict[str, object]) -> dict[str, object]:
    """Production queue entry: builds its own engine from settings."""
    from proseforge.infrastructure.database.session import (
        create_engine_and_sessionmaker,
    )
    from proseforge.settings import get_settings

    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        from proseforge.infrastructure.tasks.factory import create_task_queue

        return await execute_rollup_job(
            payload, session_factory, master_key=settings.master_key.get_secret_value(),
            queue=create_task_queue(settings, session_factory),
        )
    finally:
        await engine.dispose()


async def execute_rollup_job(payload: dict[str, object], session_factory, *, master_key: str, queue=None) -> dict[str, object]:
    from proseforge.application.work.summarize_chapter import (
        _build_provider,
        _collect_response,
    )
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

    job_id = str(payload["job_id"])
    user_id = str(payload["user_id"])

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        # Atomic claim (same discipline as the indexing worker): a duplicate
        # dispatch loses the race and skips.
        claimed = await uow.retrieval.claim_job(job_id)
        await uow.commit()
    if not claimed:
        return {"status": "skipped"}

    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            job_row = await uow.retrieval.get_job(job_id)
            if job_row is None:
                return {"status": "skipped"}
            chapter = await uow.chapters.get_owned(job_row.source_id, user_id)
            if chapter is None:
                await _finish_job(session_factory, job_id, status="failed", error="source not found")
                return {"status": "failed"}
            project_id, chapter_no = chapter.project_id, chapter.chapter_no
            spans = await load_volume_spans(uow, project_id)
            span = volume_end_span(chapter_no, spans)
            if span is None:
                # Outline changed after enqueue: nothing to roll up.
                await _finish_job(session_factory, job_id, status="done", error=None)
                return {"status": "skipped"}
            span_start, span_end = span
            index = volume_index(span, spans)
            # Volume source material: each chapter's ACTIVE version summary.
            volume_sources = await _chapter_summaries(uow, project_id, span_start, span_end)
            if not volume_sources:
                # Never write an empty recap: the chapter summaries lagging
                # behind is exactly the retryable case.
                raise RuntimeError(f"no chapter summaries for span {span_start}-{span_end}")
            from proseforge.infrastructure.database.models.recap import RecapRollupModel

            old_book = await uow.session.scalar(
                select(RecapRollupModel).where(
                    RecapRollupModel.project_id == project_id,
                    RecapRollupModel.level == "book",
                    RecapRollupModel.span_start == BOOK_SPAN_START,
                )
            )
            old_book_content = old_book.content if old_book is not None else ""
            old_book_id = old_book.id if old_book is not None else None
            era_sources: list[tuple[str, str]] = []  # (rollup id, content)
            era_span: tuple[int, int] | None = None
            if index % VOLUMES_PER_ERA == 0:
                era_span = _era_span(index, span, spans)
                era_sources = await _volume_recaps(uow, project_id, era_span, exclude_span_start=span_start)
            # Model resolution: same write-role channel as summarize_chapter.
            project = await uow.projects.get_by_id(user_id, project_id)
            locked = (project.writing_model_provider, project.writing_model_id) if project is not None and project.model_locked_at else None
            from proseforge.application.models.cluster_config import resolve_role_models

            roles = await resolve_role_models(
                uow, user_id, locked=locked,
                requested=(str(payload.get("provider", "")), str(payload.get("model", ""))),
                project_id=project_id,
            )
            provider_id, model = roles.write
            provider = None
            if provider_id and model:
                provider = await _build_provider(uow, user_id, provider_id, master_key)
            if provider is None:
                await _finish_job(session_factory, job_id, status="failed", error="模型未配置")
                return {"status": "failed"}

        # LLM round(s) outside the transaction. Any error (or an empty
        # output) raises into the retry chain below: nothing is written.
        volume_content = clip_to_budget(
            _require_content(await _collect_response(provider, GenerationRequest(
                model=model,
                system_blocks=({"role": "system", "text": _SYSTEM_PROMPT},),
                input_blocks=({"role": "user", "text": _VOLUME_PROMPT_TEMPLATE.format(
                    span_start=span_start, span_end=span_end, token_budget=VOLUME_TOKEN_BUDGET,
                    summaries="\n".join(f"第{no}章：{summary}" for no, _vid, summary in volume_sources),
                )},),
                metadata={"workflow": "recap-rollup", "role": "summarizer"},
            ))),
            VOLUME_TOKEN_BUDGET,
        )
        book_template = _BOOK_PROMPT_TEMPLATE if old_book_content else _BOOK_SEED_PROMPT_TEMPLATE
        book_content = clip_to_budget(
            _require_content(await _collect_response(provider, GenerationRequest(
                model=model,
                system_blocks=({"role": "system", "text": _SYSTEM_PROMPT},),
                input_blocks=({"role": "user", "text": book_template.format(
                    span_start=span_start, span_end=span_end, token_budget=BOOK_TOKEN_BUDGET,
                    old_book=old_book_content, new_volume=volume_content,
                )},),
                metadata={"workflow": "recap-rollup", "role": "summarizer"},
            ))),
            BOOK_TOKEN_BUDGET,
        )
        era_content = ""
        if era_span is not None and era_sources:
            era_content = clip_to_budget(
                _require_content(await _collect_response(provider, GenerationRequest(
                    model=model,
                    system_blocks=({"role": "system", "text": _SYSTEM_PROMPT},),
                    input_blocks=({"role": "user", "text": _ERA_PROMPT_TEMPLATE.format(
                        count=len(era_sources) + 1, span_start=era_span[0], span_end=era_span[1],
                        volumes="\n".join(f"第{pos}卷：{content}" for pos, (_rid, content) in enumerate(era_sources + [("", volume_content)], start=1)),
                    )},),
                    metadata={"workflow": "recap-rollup", "role": "summarizer"},
                ))),
                VOLUME_TOKEN_BUDGET,
            )

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            job_row = await uow.retrieval.get_job(job_id)
            if job_row is None:
                return {"status": "skipped"}
            now = datetime.now(UTC)
            volume_id = await _upsert_rollup(
                uow, project_id=project_id, user_id=user_id, level="volume",
                span_start=span_start, span_end=span_end, content=volume_content,
                source_ids=[version_id for _no, version_id, _summary in volume_sources], now=now,
            )
            book_id = await _upsert_rollup(
                uow, project_id=project_id, user_id=user_id, level="book",
                span_start=BOOK_SPAN_START, span_end=span_end, content=book_content,
                source_ids=[sid for sid in (old_book_id, volume_id) if sid], now=now,
            )
            era_id: str | None = None
            if era_span is not None and era_content:
                era_id = await _upsert_rollup(
                    uow, project_id=project_id, user_id=user_id, level="era",
                    span_start=era_span[0], span_end=era_span[1], content=era_content,
                    source_ids=[rid for rid, _content in era_sources] + [volume_id], now=now,
                )
            # Recaps enter the RAG archive (phase-2 item 9): one index_recap
            # job per settled recap, same commit as the content — the
            # indexing worker reads the recap row at run time, so a recap
            # re-invalidated before the job runs is never indexed.
            index_job_ids = [
                str((await uow.retrieval.enqueue_job(
                    project_id=project_id, job_type="index_recap",
                    source_type="recap_rollup", source_id=recap_id,
                )).id)
                for recap_id in [volume_id, book_id, era_id]
                if recap_id
            ]
            job_row.status = "done"
            job_row.completed_at = datetime.now(UTC)
            job_row.error = None
            await uow.commit()
        if queue is not None:
            for index_job_id in index_job_ids:
                try:
                    await queue.enqueue(
                        "proseforge.retrieval.index_document",
                        {"job_id": index_job_id, "user_id": user_id},
                    )
                except Exception:
                    # Dispatch failure strands the row in pending; the
                    # sweeper (job_type-routed) re-dispatches it later.
                    logger.exception("index_recap job %s dispatch failed; row stays pending", index_job_id)
        return {"status": "done", "level": "volume", "span": [span_start, span_end], "era": era_span is not None and bool(era_content)}
    except Exception as error:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            job_row = await uow.retrieval.get_job(job_id)
            if job_row is not None:
                if job_row.attempt >= MAX_ATTEMPTS:
                    job_row.status = "failed"
                    job_row.completed_at = datetime.now(UTC)
                else:
                    job_row.status = "pending"
                job_row.error = f"{type(error).__name__}: {error}"[:500]
            await uow.commit()
        if job_row is not None and job_row.status == "failed":
            logger.exception("rollup job %s failed permanently", job_id)
            return {"status": "failed"}
        raise


def _require_content(raw: str) -> str:
    """Empty model output is an error, never a persisted recap."""
    content = raw.strip()
    if not content:
        raise RuntimeError("model returned empty recap content")
    return content


def _era_span(index: int, span: tuple[int, int], spans: list[tuple[int, int]]) -> tuple[int, int]:
    """Chapter range of the 10-volume era ending at the current volume."""
    ordered = sorted(spans)
    if span in ordered:
        first = ordered[index - VOLUMES_PER_ERA]
        return first[0], span[1]
    start = (index - VOLUMES_PER_ERA) * VOLUME_FALLBACK_CHAPTERS + 1
    return start, span[1]


async def _chapter_summaries(uow, project_id: str, span_start: int, span_end: int) -> list[tuple[int, str, str]]:
    """(chapter_no, version_id, summary) for chapters in the span whose
    ACTIVE version carries a summary, ordered by chapter number."""
    from proseforge.infrastructure.database.models.chapter import (
        ChapterModel,
        ChapterVersionModel,
    )

    rows = await uow.session.execute(
        select(ChapterModel.chapter_no, ChapterVersionModel.id, ChapterVersionModel.summary)
        .join(ChapterVersionModel, ChapterVersionModel.id == ChapterModel.active_version_id)
        .where(
            ChapterModel.project_id == project_id,
            ChapterModel.chapter_no >= span_start,
            ChapterModel.chapter_no <= span_end,
            ChapterVersionModel.summary != "",
        )
        .order_by(ChapterModel.chapter_no)
    )
    return [(int(no), str(version_id), str(summary)) for no, version_id, summary in rows.all()]


async def _volume_recaps(uow, project_id: str, era_span: tuple[int, int], *, exclude_span_start: int) -> list[tuple[str, str]]:
    """(rollup id, content) of existing volume recaps inside the era span,
    oldest first. The just-produced volume is excluded — the caller
    appends its in-memory content."""
    from proseforge.infrastructure.database.models.recap import RecapRollupModel

    rows = await uow.session.scalars(
        select(RecapRollupModel)
        .where(
            RecapRollupModel.project_id == project_id,
            RecapRollupModel.level == "volume",
            RecapRollupModel.span_start >= era_span[0],
            RecapRollupModel.span_end <= era_span[1],
            RecapRollupModel.span_start != exclude_span_start,
            RecapRollupModel.content != "",
        )
        .order_by(RecapRollupModel.span_start)
    )
    return [(str(row.id), str(row.content)) for row in rows]


async def _upsert_rollup(
    uow, *, project_id: str, user_id: str, level: str,
    span_start: int, span_end: int, content: str,
    source_ids: list[str], now: datetime,
) -> str:
    """Insert or rewrite the (project, level, span_start) row; regeneration
    clears the stale flag. Emits a recap.rollup audit event per write —
    every compression is auditable, nothing silently changes."""
    from proseforge.infrastructure.database.models.recap import RecapRollupModel
    from proseforge.infrastructure.database.models.remaining import AuditLogModel

    row = await uow.session.scalar(
        select(RecapRollupModel).where(
            RecapRollupModel.project_id == project_id,
            RecapRollupModel.level == level,
            RecapRollupModel.span_start == span_start,
        )
    )
    if row is None:
        row = RecapRollupModel(
            id=new_id(), project_id=project_id, user_id=user_id, level=level,
            span_start=span_start, span_end=span_end, content=content,
            source_version_ids=json.dumps(source_ids, ensure_ascii=False),
            stale=False, created_at=now, updated_at=now,
        )
        uow.session.add(row)
        await uow.session.flush()
    else:
        row.span_end = span_end
        row.content = content
        row.source_version_ids = json.dumps(source_ids, ensure_ascii=False)
        row.stale = False
        row.updated_at = now
    uow.session.add(AuditLogModel(
        id=new_id(), user_id=user_id, action="recap.rollup",
        target_type="recap_rollup", target_id=row.id,
        payload=json.dumps({
            "project_id": project_id, "level": level,
            "span_start": span_start, "span_end": span_end,
            "token_estimate": estimate_tokens(content), "sources": len(source_ids),
        }, ensure_ascii=False, separators=(",", ":")),
    ))
    return str(row.id)


async def _finish_job(session_factory, job_id: str, *, status: str, error: str | None) -> None:
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        job_row = await uow.retrieval.get_job(job_id)
        if job_row is not None:
            job_row.status = status
            job_row.error = error
            if status in {"done", "failed"}:
                job_row.completed_at = datetime.now(UTC)
        await uow.commit()
