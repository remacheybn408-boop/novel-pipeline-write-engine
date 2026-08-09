"""Chapter summary + character extraction worker.

Mounted like the retrieval indexing job: proposal approval enqueues a
retrieval_jobs row (job_type="summarize_chapter", source_type=
"chapter_version", source_id=version id) and the route dispatches this
task after commit. Idempotent: a version that already has a summary is
skipped. The locked writing model (cluster write role) makes ONE
single-round call asking for strict JSON; parsing is defensive — on
failure the raw output becomes the summary and no characters are
extracted, the job never blows up. Missing credential/model config fails
the job without raising (the approval chain must never break); other
errors re-arm pending until MAX_ATTEMPTS, re-raising for queue backoff.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import UTC, datetime

from proseforge.domain.ports.model_provider import GenerationRequest

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MAX_CHARACTERS = 10
MAX_FACTS = 15
# Defensive prompt budget: chapters can be long; the summarizer does not
# need the full text to produce a 120-200 char abstract.
MAX_CONTENT_CHARS = 20000
SUMMARY_FALLBACK_CHARS = 200

_SYSTEM_PROMPT = "你是小说编辑助手。只输出严格 JSON，不要输出任何解释或 Markdown 标记。"

_USER_PROMPT_TEMPLATE = """以下是小说第{chapter_no}章《{title}》的正文。完成五件事：
1. 写一段 120-200 字的本章浓缩摘要，摘要必须明确覆盖四点：主线进展、人物状态变化、未结伏笔、关键道具与地点；
2. 提取本章新出场或有重要戏份的角色（最多 10 个），每个角色给出姓名、别名列表、一句话简介、戏份定位（如 主角/反派/配角）；
3. 提取本章涉及的、与已有角色或已有设定条目有关的关键事实（最多 15 条），每条给出实体名、字段名（如 角色/别名/状态/地点/关系）和简短值（不超过 50 字）。已有角色与设定条目见下方清单，不要提取清单之外的实体。
4. 提取本章的章级事实 chapter_fact：timeline（本章时间线一句话）、locations（出场角色到所在地点的对象）、items（关键道具到持有人或位置的对象）、revealed（本章新揭示的信息列表）、open_loops（本章遗留的未结伏笔/悬念列表，如 戒指的来历、师父的身份；没有把握的留空数组）、time_anchor（时间锚点，如 三日后/当夜）；没有把握的字段留空数组或空对象；
5. 提取出场角色的精神状态 character_states（最多 10 个）：name（角色名）、emotion（情绪，如 开心/悲伤/愤怒/焦虑/平静）、mental（精神状态标签，如 正常/抑郁/创伤后应激/妄想/失眠）、note（一句话说明）；name 须来自已有角色或本章提取的角色。

已有角色：{known_characters}
已有设定条目：{known_facts}

只返回如下 JSON：
{{"summary": "...", "characters": [{{"name": "...", "aliases": ["..."], "summary": "...", "role": "..."}}], "facts": [{{"entity": "...", "field": "...", "value": "..."}}], "chapter_fact": {{"timeline": "...", "locations": {{"角色": "地点"}}, "items": {{"道具": "持有人"}}, "revealed": ["..."], "open_loops": ["..."], "time_anchor": "..."}}, "character_states": [{{"name": "...", "emotion": "...", "mental": "...", "note": "..."}}]}}

正文：
{content}"""


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    match = re.match(r"^```(?:json)?\s*(?P<body>.*?)\s*```$", stripped, re.DOTALL | re.IGNORECASE)
    return match.group("body") if match else stripped


def _str_map(value: object) -> dict[str, str]:
    """Keep only str->str entries of a mapping (defensive extraction)."""
    if not isinstance(value, dict):
        return {}
    return {
        str(key).strip(): str(item).strip()
        for key, item in value.items()
        if str(key).strip() and str(item).strip()
    }


def parse_summary_payload(raw: str) -> tuple[str, list[dict[str, object]], list[dict[str, object]], dict[str, object], list[dict[str, object]]]:
    """Defensive parse. Returns (summary, characters, facts, chapter_fact,
    character_states); on any JSON failure the cleaned raw text (truncated)
    becomes the summary with everything else empty, so a flaky model never
    fails the job. A broken facts/state section alone never costs the
    summary or characters.
    """
    candidate = _strip_code_fence(raw)
    start = candidate.find("{")
    data: dict[str, object] | None = None
    if start != -1:
        try:
            parsed, _end = json.JSONDecoder().raw_decode(candidate[start:])
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            data = None
    if data is None:
        return candidate[:SUMMARY_FALLBACK_CHARS].strip(), [], [], {}, []
    summary = str(data.get("summary", "")).strip()
    characters: list[dict[str, object]] = []
    raw_characters = data.get("characters", [])
    if isinstance(raw_characters, list):
        for item in raw_characters[:MAX_CHARACTERS]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            aliases = item.get("aliases", [])
            characters.append({
                "name": name,
                "aliases": [str(alias).strip() for alias in aliases if str(alias).strip()] if isinstance(aliases, list) else [],
                "summary": str(item.get("summary", "")).strip(),
                "role": str(item.get("role", "")).strip(),
            })
    facts: list[dict[str, object]] = []
    raw_facts = data.get("facts", [])
    if isinstance(raw_facts, list):
        for item in raw_facts[:MAX_FACTS]:
            if not isinstance(item, dict):
                continue
            entity = str(item.get("entity", "")).strip()
            field = str(item.get("field", "")).strip()
            value = str(item.get("value", "")).strip()
            if entity and field and value:
                facts.append({"entity": entity, "field": field, "value": value[:50]})
    # Chapter-level fact snapshot: every field optional, wrong shapes drop
    # out field-by-field instead of sinking the whole block.
    chapter_fact: dict[str, object] = {}
    raw_chapter_fact = data.get("chapter_fact")
    if isinstance(raw_chapter_fact, dict):
        timeline = str(raw_chapter_fact.get("timeline", "")).strip()
        if timeline:
            chapter_fact["timeline"] = timeline
        locations = _str_map(raw_chapter_fact.get("locations"))
        if locations:
            chapter_fact["locations"] = locations
        items = _str_map(raw_chapter_fact.get("items"))
        if items:
            chapter_fact["items"] = items
        raw_revealed = raw_chapter_fact.get("revealed", [])
        if isinstance(raw_revealed, list):
            revealed = [str(entry).strip() for entry in raw_revealed if str(entry).strip()]
            if revealed:
                chapter_fact["revealed"] = revealed
        raw_open_loops = raw_chapter_fact.get("open_loops", [])
        if isinstance(raw_open_loops, list):
            open_loops = [str(entry).strip() for entry in raw_open_loops if str(entry).strip()]
            if open_loops:
                chapter_fact["open_loops"] = open_loops
        time_anchor = str(raw_chapter_fact.get("time_anchor", "")).strip()
        if time_anchor:
            chapter_fact["time_anchor"] = time_anchor
    character_states: list[dict[str, object]] = []
    raw_states = data.get("character_states", [])
    if isinstance(raw_states, list):
        for item in raw_states[:MAX_CHARACTERS]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            character_states.append({
                "name": name,
                "emotion": str(item.get("emotion", "")).strip(),
                "mental": str(item.get("mental", "")).strip(),
                "note": str(item.get("note", "")).strip(),
            })
    return summary, characters, facts, chapter_fact, character_states


async def _collect_response(provider, request: GenerationRequest) -> str:
    parts: list[str] = []
    async for event in provider.stream(request):
        if event.event == "content.delta":
            parts.append(event.text)
    return "".join(parts).strip()


async def run_summarize_job(payload: dict[str, object]) -> dict[str, object]:
    """Production queue entry: builds its own engine from settings."""
    from proseforge.infrastructure.database.session import (
        create_engine_and_sessionmaker,
    )
    from proseforge.settings import get_settings

    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        from proseforge.infrastructure.tasks.factory import create_task_queue

        return await execute_summarize_job(
            payload, session_factory, master_key=settings.master_key.get_secret_value(),
            queue=create_task_queue(settings, session_factory),
        )
    finally:
        await engine.dispose()


async def execute_summarize_job(payload: dict[str, object], session_factory, *, master_key: str, queue=None) -> dict[str, object]:
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

    job_id = str(payload["job_id"])
    user_id = str(payload["user_id"])

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        job = await uow.retrieval.get_job(job_id)
        if job is None or job.status in {"done", "failed"}:
            return {"status": "skipped"}
        job.status = "running"
        job.attempt += 1
        job.started_at = datetime.now(UTC)
        job.error = None
        await uow.commit()

    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            job_row = await uow.retrieval.get_job(job_id)
            if job_row is None:
                return {"status": "skipped"}
            version = await uow.chapters.get_version(job_row.source_id)
            if version is None:
                await _finish_job(session_factory, job_id, status="failed", error="source not found")
                return {"status": "failed"}
            if version.summary:
                # Idempotency: this version is already summarized.
                await _finish_job(session_factory, job_id, status="done", error=None)
                return {"status": "skipped"}
            chapter = await uow.chapters.get_owned(version.chapter_id, user_id)
            if chapter is None:
                await _finish_job(session_factory, job_id, status="failed", error="source not found")
                return {"status": "failed"}
            project = await uow.projects.get_by_id(user_id, chapter.project_id)
            locked = (project.writing_model_provider, project.writing_model_id) if project is not None and project.model_locked_at else None
            from proseforge.application.models.cluster_config import resolve_role_models

            roles = await resolve_role_models(
                uow, user_id, locked=locked,
                requested=(str(payload.get("provider", "")), str(payload.get("model", ""))),
                project_id=chapter.project_id,
            )
            provider_id, model = roles.write
            provider = None
            if provider_id and model:
                provider = await _build_provider(uow, user_id, provider_id, master_key)
            if provider is None:
                await _finish_job(session_factory, job_id, status="failed", error="模型未配置")
                return {"status": "failed"}
            # Detach plain values before the session closes.
            version_id, content = version.id, version.content
            chapter_id, chapter_no, chapter_title = chapter.id, chapter.chapter_no, chapter.title
            project_id = chapter.project_id
            # Known entities for the facts section of the prompt.
            from sqlalchemy import select as _select

            from proseforge.infrastructure.database.models.story_bible import (
                StoryBibleEntryModel,
            )

            known_characters = "、".join(
                character.name for character in await uow.characters.list_for_project(project_id)
            ) or "（无）"
            story_keys = (await uow.session.scalars(
                _select(StoryBibleEntryModel.key).where(StoryBibleEntryModel.project_id == project_id)
            )).all()
            known_facts = "、".join(str(key) for key in story_keys) or "（无）"

        request = GenerationRequest(
            model=model,
            system_blocks=({"role": "system", "text": _SYSTEM_PROMPT},),
            input_blocks=({"role": "user", "text": _USER_PROMPT_TEMPLATE.format(
                chapter_no=chapter_no, title=chapter_title, content=content[:MAX_CONTENT_CHARS],
                known_characters=known_characters, known_facts=known_facts,
            )},),
            metadata={"workflow": "chapter-summary", "role": "summarizer"},
        )
        raw = await _collect_response(provider, request)
        summary, extracted, facts, chapter_fact, character_states = parse_summary_payload(raw)

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            job_row = await uow.retrieval.get_job(job_id)
            if job_row is None:
                return {"status": "skipped"}
            await uow.chapters.set_version_summary(version_id, summary)
            for item in extracted:
                await uow.characters.merge_extracted(
                    project_id,
                    name=str(item["name"]),
                    aliases=list(item["aliases"]),
                    summary=str(item["summary"]),
                    role=str(item["role"]),
                    chapter_no=chapter_no,
                )
            conflicts = 0
            try:
                # Heuristic conflict collection: evidence rows only, never
                # blocks or alters the summarizer's writes.
                from proseforge.application.work.conflict_check import check_conflicts

                conflicts = await check_conflicts(
                    uow, project_id=project_id, version_id=version_id, chapter_no=chapter_no, facts=facts
                )
            except Exception:
                logger.exception("conflict check failed job_id=%s; continuing without it", job_id)
            try:
                # State ledger writes (chapter_fact / character_state):
                # best-effort like the conflict check, never blocks the job.
                await _persist_state_entries(
                    uow, project_id=project_id, chapter_no=chapter_no,
                    chapter_fact=chapter_fact, character_states=character_states,
                )
                # 记忆优先写入侧（先查再想）：显著事实沉淀为自动激活的
                # 项目级记忆，下一章所有角色调模型前即可查到。
                await _propose_pipeline_memories(
                    uow, project_id=project_id, chapter_id=chapter_id, chapter_no=chapter_no,
                    chapter_fact=chapter_fact, character_states=character_states,
                )
            except Exception:
                logger.exception("state entry persist failed job_id=%s; continuing without it", job_id)
            job_row.status = "done"
            job_row.completed_at = datetime.now(UTC)
            job_row.error = None
            await uow.commit()
        rollup_job_id = await _enqueue_rollup_if_volume_end(
            session_factory, project_id=project_id, chapter_id=chapter_id, chapter_no=chapter_no
        )
        if rollup_job_id is not None and queue is not None:
            try:
                await queue.enqueue(
                    "proseforge.work.rollup_recap",
                    {"job_id": rollup_job_id, "user_id": user_id},
                )
            except Exception:
                logger.exception("rollup job %s dispatch failed; row stays pending", rollup_job_id)
        return {"status": "done", "characters": len(extracted), "conflicts": conflicts}
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
            logger.exception("summarize job %s failed permanently", job_id)
            return {"status": "failed"}
        raise


async def _enqueue_rollup_if_volume_end(session_factory, *, project_id: str, chapter_id: str, chapter_no: int) -> str | None:
    """Memory-pyramid trigger: when this chapter closes its volume, enqueue
    a rollup_recap job (its own commit, right after the summary commit).
    Best-effort like the conflict check — a rollup bookkeeping failure
    must never overturn the summary that already landed."""
    from proseforge.application.work.rollup_recap import maybe_enqueue_rollup
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            job_id = await maybe_enqueue_rollup(uow, project_id=project_id, chapter_id=chapter_id, chapter_no=chapter_no)
            await uow.commit()
            return job_id
    except Exception:
        logger.exception("rollup enqueue failed project=%s chapter=%s", project_id, chapter_no)
        return None


async def _upsert_state_entry(uow, *, project_id: str, kind: str, key: str, value: dict[str, object]) -> None:
    """Insert the entry or update the existing same-key row in place
    (optimistic-lock version bumps, same discipline as the story-bible API).
    New values merge over the stored value_json so partial re-extractions
    keep older fields."""
    from sqlalchemy import select as _select

    from proseforge.domain.story_bible.entities import StoryFact
    from proseforge.infrastructure.database.models.story_bible import (
        StoryBibleEntryModel,
    )

    now = datetime.now(UTC)
    row = await uow.session.scalar(_select(StoryBibleEntryModel).where(
        StoryBibleEntryModel.project_id == project_id,
        StoryBibleEntryModel.kind == kind,
        StoryBibleEntryModel.key == key,
    ))
    if row is None:
        fact = StoryFact.create(project_id, kind, key, value, source="auto", confidence=0.6)
        uow.session.add(StoryBibleEntryModel(
            id=fact.id, project_id=project_id, kind=kind, key=key,
            value_json=json.dumps(fact.value, ensure_ascii=False),
            status=fact.status, confidence=fact.confidence, source="auto",
            pinned=False, version=1, created_at=now, updated_at=now,
        ))
        return
    try:
        stored = json.loads(row.value_json or "{}")
    except ValueError:
        stored = {}
    merged = {**stored, **value}
    row.value_json = json.dumps(merged, ensure_ascii=False)
    row.version = int(row.version) + 1
    row.updated_at = now


async def _persist_state_entries(uow, *, project_id: str, chapter_no: int, chapter_fact: dict[str, object], character_states: list[dict[str, object]]) -> None:
    """Write the auto-extracted state ledger: one chapter_fact row per
    chapter (key=f"ch{chapter_no}") and one character_state row per
    character (key=character name, latest state wins)."""
    if chapter_fact:
        await _upsert_state_entry(
            uow, project_id=project_id, kind="chapter_fact", key=f"ch{chapter_no}",
            value={"chapter_no": chapter_no, **chapter_fact},
        )
    for state in character_states:
        name = str(state.get("name", "")).strip()
        if not name:
            continue
        await _upsert_state_entry(
            uow, project_id=project_id, kind="character_state", key=name,
            value={
                "emotion": str(state.get("emotion", "")).strip(),
                "mental": str(state.get("mental", "")).strip(),
                "note": str(state.get("note", "")).strip(),
                "chapter_no": chapter_no,
            },
        )


# 管线记忆候选的每章上限：人物状态/道具/地点各自封顶，防止单章刷屏挤占切片。
_PIPELINE_MEMORY_MAX_STATES = 6
_PIPELINE_MEMORY_MAX_ITEMS = 4
_PIPELINE_MEMORY_MAX_LOCATIONS = 4


async def _propose_pipeline_memories(
    uow,
    *,
    project_id: str,
    chapter_id: str,
    chapter_no: int,
    chapter_fact: dict[str, object],
    character_states: list[dict[str, object]],
) -> None:
    """章节显著事实 → 自动激活的项目级记忆（记忆优先的写入侧）。

    与 story_bible 台账同批沉淀：人物状态/时间锚/关键道具/所在地点按 key
    滚动（revision 递增，切片只留最新），重大揭示按章留档。全部直接
    ACCEPTED（管线事实不走人工审批，否则批量链被审批卡死）；来源以
    run_id=PROJECT_WIDE_RUN + source_artifact_id=chapter_id 记账可回查。
    open_loops 不重复沉淀——伏笔闭环是奥莉维亚承诺台账的职责。
    """
    from proseforge.application.agents.memory_service import (
        PROJECT_WIDE_RUN,
        invalidate_memory_slice_cache,
        propose_memory,
    )

    proposed = 0
    for state in character_states[:_PIPELINE_MEMORY_MAX_STATES]:
        name = str(state.get("name", "")).strip()
        if not name:
            continue
        parts = [part for part in (
            f"情绪：{str(state.get('emotion', '')).strip()}" if str(state.get("emotion", "")).strip() else "",
            f"精神：{str(state.get('mental', '')).strip()}" if str(state.get("mental", "")).strip() else "",
            str(state.get("note", "")).strip(),
        ) if part]
        if not parts:
            continue
        await propose_memory(
            uow.session, project_id=project_id, run_id=PROJECT_WIDE_RUN,
            memory_key=f"人物状态·{name}", value=f"第{chapter_no}章：" + "；".join(parts),
            source_artifact_id=chapter_id, confidence=0.6, status="ACCEPTED",
        )
        proposed += 1
    time_anchor = str(chapter_fact.get("time_anchor", "")).strip() if chapter_fact else ""
    if time_anchor:
        await propose_memory(
            uow.session, project_id=project_id, run_id=PROJECT_WIDE_RUN,
            memory_key="时间线锚点", value=f"第{chapter_no}章：{time_anchor}",
            source_artifact_id=chapter_id, confidence=0.6, status="ACCEPTED",
        )
        proposed += 1
    items = chapter_fact.get("items") if chapter_fact else None
    if isinstance(items, dict):
        for item_name, holder in list(items.items())[:_PIPELINE_MEMORY_MAX_ITEMS]:
            await propose_memory(
                uow.session, project_id=project_id, run_id=PROJECT_WIDE_RUN,
                memory_key=f"关键道具·{item_name}", value=f"第{chapter_no}章：{holder}",
                source_artifact_id=chapter_id, confidence=0.6, status="ACCEPTED",
            )
            proposed += 1
    locations = chapter_fact.get("locations") if chapter_fact else None
    if isinstance(locations, dict):
        for person, place in list(locations.items())[:_PIPELINE_MEMORY_MAX_LOCATIONS]:
            await propose_memory(
                uow.session, project_id=project_id, run_id=PROJECT_WIDE_RUN,
                memory_key=f"所在地点·{person}", value=f"第{chapter_no}章：{place}",
                source_artifact_id=chapter_id, confidence=0.6, status="ACCEPTED",
            )
            proposed += 1
    revealed = chapter_fact.get("revealed") if chapter_fact else None
    if isinstance(revealed, list) and revealed:
        await propose_memory(
            uow.session, project_id=project_id, run_id=PROJECT_WIDE_RUN,
            memory_key=f"重大揭示·ch{chapter_no}", value="；".join(str(entry) for entry in revealed[:3]),
            source_artifact_id=chapter_id, confidence=0.6, status="ACCEPTED",
        )
        proposed += 1
    if proposed:
        # 切片缓存 2s TTL 兜底跨进程可见；同进程立即使新记忆可查。
        invalidate_memory_slice_cache()
        logger.info("pipeline memories proposed project=%s chapter=%s count=%d", project_id, chapter_no, proposed)


async def _build_provider(uow, user_id: str, provider_id: str, master_key: str):
    from proseforge.infrastructure.security.credential_cipher import (
        CredentialCipher,
        derive_key,
    )
    from proseforge.providers.factory import build_provider

    credential = await uow.credentials.get_for_user(user_id, provider_id)
    if credential is None:
        return None
    associated = f"{user_id}:{provider_id}:{credential.id}".encode()
    secret = json.loads(
        CredentialCipher(derive_key(master_key)).decrypt(
            base64.b64decode(credential.encrypted_payload), associated_data=associated
        )
    )
    try:
        return build_provider(provider_id, secret["api_key"], secret.get("base_url"))
    except KeyError:
        return None


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
