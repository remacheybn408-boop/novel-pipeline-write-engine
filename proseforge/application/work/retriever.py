"""NarrativeRetriever: four-section scene pack for work-mode writing.

Sections in priority order (authority: pinned > Story Bible > adopted
chapters > drafts > auto-extracted characters last):
1. [世界观与设定] — pinned story-bible entries (all), then active entries
   by confidence desc, then characters (source=user before source=auto);
2. [当前状态] — build_scene_state (settled volume/book/era recaps first —
   stale recaps are filtered out at query time — then latest chapter /
   last-5 summaries / on-scene characters / open promises);
3. [写作约束] — caller-supplied constraints (viewpoint, chapter goal...);
4. [长篇事实证据] — hybrid retrieval evidence blocks, annotated 第N章
   (recap documents surface by title instead). Fusion is authority-layered
   (rrf_fuse_with_authority): canon chunks always rank above derived
   recap chunks — a derived recap never outranks the原文.

Building a pack for the next chapter also triggers the memory-pyramid
lazy recompute (phase-2 item 8): stale volume recaps get a rollup job
enqueued + dispatched here, so the pyramid heals itself before the write.

Budget: structured sections share STRUCTURED_BUDGET_TOKENS and are
trimmed lowest-priority-item-first; evidence fills up to
EVIDENCE_BUDGET_TOKENS by fused score. Token estimate: len//2 (CJK proxy,
same as the rest of the codebase). Every call persists a retrieval_runs
snapshot. Chat-mode projects call this too when the narrative-RAG switch
is on (the wiring no longer gates retrieval on project mode).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from proseforge.application.retrieval.indexing import _resolve_embedding_engine
from proseforge.application.retrieval.search import (
    EvidenceBlock,
    expand_neighbors,
    keyword_leg,
    rrf_fuse_with_authority,
    vector_leg,
)
from proseforge.application.work.rollup_recap import enqueue_stale_recap_recompute
from proseforge.application.work.scene_state import build_scene_state
from proseforge.domain.characters.matching import match_characters
from proseforge.domain.common.ids import new_id
from proseforge.domain.story_bible.entities import RETRIEVABLE_STATUSES
from proseforge.infrastructure.database.models.retrieval import RetrievalRunModel
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel

logger = logging.getLogger(__name__)

NARRATIVE_RAG_SKILL_KEY = "builtin-narrative-rag"

STRUCTURED_BUDGET_TOKENS = 6000
EVIDENCE_BUDGET_TOKENS = 16000

# A retrieval query is a search key, not a document: cap its length so a
# pasted full chapter cannot blow the embedding request (API embedders
# bill/reject on the full text) or flood the pack's constraints section.
QUERY_MAX_CHARS = 2000


async def narrative_rag_switch_enabled(uow, user_id: str) -> bool:
    """True unless the user explicitly disabled builtin-narrative-rag.

    NOTE: default is ON for this switch (no state row = enabled); the
    web-search switch uses the inverse default (no row = disabled).
    """
    if not user_id:
        return False
    for state in await uow.builtin_skill_states.list_for_user(user_id):
        if state.skill_key == NARRATIVE_RAG_SKILL_KEY:
            return bool(state.enabled)
    return True


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 2)


SECTION_META = (
    ("worldview", "[世界观与设定]"),
    ("current_state", "[当前状态]"),
    ("constraints", "[写作约束]"),
    ("evidence", "[长篇事实证据]"),
)


def render_pack_text(sections: dict[str, str]) -> str:
    """Sections dict -> pack text (assembly order = priority order)."""
    return "\n\n".join(
        f"{title}\n{sections[key]}" for key, title in SECTION_META if sections.get(key)
    )


def trim_scene_pack(sections: dict[str, str], budget_tokens: int) -> str:
    """Trim a pack to a token budget, lowest priority first: trailing
    evidence blocks, then whole sections in reverse priority order, and
    the worldview section hard-truncated only as the last resort."""
    trimmed = dict(sections)
    while trimmed.get("evidence") and _estimate_tokens(render_pack_text(trimmed)) > budget_tokens:
        lines = trimmed["evidence"].split("\n\n")
        lines.pop()
        trimmed["evidence"] = "\n\n".join(lines)
    for key, _title in reversed(SECTION_META):
        if _estimate_tokens(render_pack_text(trimmed)) <= budget_tokens:
            break
        trimmed[key] = ""
    while trimmed.get("worldview") and _estimate_tokens(render_pack_text(trimmed)) > budget_tokens:
        trimmed["worldview"] = trimmed["worldview"][: max(0, len(trimmed["worldview"]) - 200)]
    return render_pack_text(trimmed)


# Auto-extracted state-ledger kinds render in [当前状态] via scene_state,
# never as worldview entries.
STATE_LEDGER_KINDS = frozenset({"chapter_fact", "character_state"})


@dataclass(frozen=True)
class ScenePack:
    text: str
    sections: dict[str, str]
    evidence: list[EvidenceBlock]
    run_id: str
    token_cost: int


def _format_story_entry(kind: str, key: str, value_json: str) -> str:
    try:
        value = json.loads(value_json or "{}")
    except ValueError:
        value = {}
    note = value.get("note") or value.get("description") or json.dumps(value, ensure_ascii=False)
    return f"- [{kind}] {key}：{note}"


def _format_character(character, voice: Mapping[str, object] | None = None) -> str:
    aliases = f"（别名：{'、'.join(character.aliases)}）" if character.aliases else ""
    role = f"｜{character.role}" if character.role else ""
    summary = f"：{character.summary}" if character.summary else ""
    auto = "（自动提取）" if character.source == "auto" else ""
    line = f"- {character.name}{aliases}{role}{summary}{auto}"
    voice_parts = _voice_parts(voice) if voice else []
    if voice_parts:
        line += f"（声纹：{'；'.join(voice_parts)}）"
    return line


def _voice_parts(voice: Mapping[str, object]) -> list[str]:
    """Short voice rendering: sentence style, dialect, catchphrases,
    emotional baseline. Only fields actually present are rendered."""
    parts: list[str] = []
    sentence_len = voice.get("sentence_len")
    if isinstance(sentence_len, list) and len(sentence_len) == 2:
        parts.append(f"句式{sentence_len[0]}-{sentence_len[1]}字")
    dialect = voice.get("dialect")
    if isinstance(dialect, str) and dialect.strip():
        parts.append(f"方言：{dialect.strip()}")
    catchphrases = voice.get("catchphrases")
    if isinstance(catchphrases, list) and catchphrases:
        parts.append(f"口头禅：{'、'.join(str(item) for item in catchphrases[:5] if str(item).strip())}")
    emotion_baseline = voice.get("emotion_baseline")
    if isinstance(emotion_baseline, str) and emotion_baseline.strip():
        parts.append(f"情绪基线：{emotion_baseline.strip()}")
    return parts


def _format_scene_state(state: dict[str, object]) -> str:
    lines: list[str] = []
    # Memory pyramid first: settled volume/book/era recaps (stale rows are
    # filtered out in scene_state, so nothing invalidated reaches here).
    recap_labels = {"volume": "卷梗概", "book": "全书梗概", "era": "部梗概"}
    for recap in state.get("recaps", []):
        label = recap_labels.get(str(recap["level"]), "梗概")
        lines.append(f"{label}（第{recap['span_start']}-{recap['span_end']}章）：{recap['content']}")
    latest = state.get("latest_chapter")
    if latest:
        summary = f"：{latest['summary']}" if latest.get("summary") else ""
        lines.append(f"最新章 第{latest['no']}章《{latest['title']}》{summary}")
    for item in state.get("recent_summaries", []):
        if latest and item["no"] == latest["no"]:
            continue
        if item.get("summary"):
            lines.append(f"第{item['no']}章：{item['summary']}")
    if state.get("characters"):
        names = "、".join(c["name"] for c in state["characters"])
        lines.append(f"最新章出场：{names}")
    character_states = state.get("character_states") or []
    if character_states:
        segments: list[str] = []
        for item in character_states:
            value = item["value"]
            label = "/".join(part for part in (str(value.get("emotion", "")).strip(), str(value.get("mental", "")).strip()) if part)
            note = str(value.get("note", "")).strip()
            suffix = f"（{note}）" if note else ""
            segments.append(f"{item['key']}：{label}{suffix}" if label else f"{item['key']}{suffix}")
        lines.append(f"角色精神状态：{'；'.join(segments)}")
    for fact in state.get("chapter_facts", []):
        value = fact["value"]
        bits: list[str] = []
        timeline = str(value.get("timeline", "")).strip()
        if timeline:
            bits.append(f"时间线：{timeline}")
        items = value.get("items")
        if isinstance(items, dict) and items:
            bits.append("道具：" + "、".join(f"{prop}→{holder}" for prop, holder in list(items.items())[:5]))
        revealed = value.get("revealed")
        if isinstance(revealed, list) and revealed:
            bits.append("揭示：" + "、".join(str(entry) for entry in revealed[:3]))
        if bits:
            lines.append(f"第{fact['chapter_no']}章事实：{'；'.join(bits)}")
    for promise in state.get("open_promises", []):
        note = promise["value"].get("note") or promise["key"]
        lines.append(f"未结伏笔：{promise['key']}（{note}）")
    return "\n".join(lines)


def _pack_budget(items: list[str], budget_tokens: int) -> tuple[list[str], list[str]]:
    """Keep items in priority order while they fit; drop lower-priority
    items (later in the list) first. Returns (kept, dropped)."""
    kept: list[str] = []
    dropped: list[str] = []
    used = 0
    for item in items:
        cost = _estimate_tokens(item)
        if used + cost > budget_tokens:
            dropped.append(item)
            continue
        kept.append(item)
        used += cost
    return kept, dropped


class NarrativeRetriever:
    def __init__(self, session_factory, *, master_key: str):
        self.session_factory = session_factory
        self.master_key = master_key

    async def build(
        self, *, project_id: str, user_id: str, query: str,
        chapter_no: int | None = None, constraints: list[str] | None = None,
        intent: str = "scene_pack",
        conversation_id: str | None = None, message_id: str | None = None,
    ) -> ScenePack:
        from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

        started = time.monotonic()
        async with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            session = uow.session

            # -- retrieval legs ------------------------------------------------
            engine = await _resolve_embedding_engine(uow, user_id, self.master_key)
            legs = []
            query_vector = None
            if engine is not None and engine.embedder is not None and query.strip():
                # Query side: e5-family local models take a "query: " prefix
                # (passage prefix is for indexing only); API embedders treat
                # embed_query as a plain embed.
                embedded = await engine.embedder.embed_query([query])
                query_vector = embedded.vectors[0] if embedded.vectors else None
            if query_vector is not None:
                legs.append(await vector_leg(
                    session, project_id=project_id, query_vector=query_vector, identity=engine.identity
                ))
            characters = await uow.characters.list_for_project(project_id)
            mentioned = match_characters(query, characters)
            entities = [term for character in mentioned for term in [character.name, *character.aliases]]
            legs.append(await keyword_leg(
                session, project_id=project_id, query=query, entities=entities,
                identity=engine.identity if engine is not None else None,
            ))
            fused = await rrf_fuse_with_authority(session, legs)
            evidence = await expand_neighbors(session, fused)

            # Empty evidence with chapters that SHOULD be indexed means the
            # index pipeline is silently broken (the read side always
            # "succeeds" — 23 chapters once shipped with an empty index
            # unnoticed). Make it loud: alert log + marker in retrieval_runs.
            empty_index: dict[str, int] | None = None
            if not evidence:
                indexable = await uow.retrieval.count_indexable_chapters_for_project(project_id=project_id)
                active_chunks = await uow.retrieval.count_active_chunks_for_project(project_id=project_id)
                if indexable > 0:
                    empty_index = {"indexable_chapters": indexable, "active_chunks": active_chunks}
                    logger.warning(
                        "rag.empty_index project_id=%s: %d chapters should be indexed, "
                        "%d active chunks, 0 evidence blocks — index pipeline broken or drifting",
                        project_id, indexable, active_chunks,
                    )

            # -- section 1: worldview ------------------------------------------
            # Guard: only retrievable statuses enter the pack — excluded
            # rows and terminal promise states never come back.
            story_rows = list((await session.scalars(
                select(StoryBibleEntryModel).where(
                    StoryBibleEntryModel.project_id == project_id,
                    StoryBibleEntryModel.status.in_(RETRIEVABLE_STATUSES),
                )
            )).all())
            # Voice profiles ride on kind=character entries: key = character
            # name, value.voice = the validated voice profile.
            voice_by_name: dict[str, Mapping[str, object]] = {}
            for row in story_rows:
                if row.kind != "character":
                    continue
                try:
                    character_value = json.loads(row.value_json or "{}")
                except ValueError:
                    continue
                voice = character_value.get("voice") if isinstance(character_value, dict) else None
                if isinstance(voice, Mapping):
                    voice_by_name[row.key] = voice
            worldview_rows = [row for row in story_rows if row.kind not in STATE_LEDGER_KINDS]
            pinned_items = [
                _format_story_entry(row.kind, row.key, row.value_json) for row in worldview_rows if row.pinned
            ]
            other_items = [
                _format_story_entry(row.kind, row.key, row.value_json)
                for row in sorted(
                    (row for row in worldview_rows if not row.pinned),
                    key=lambda row: row.confidence, reverse=True,
                )
            ]
            character_items = [
                _format_character(character, voice_by_name.get(character.name))
                for character in sorted(characters, key=lambda c: (c.source != "user", c.name))
            ]
            worldview_lines, worldview_dropped = _pack_budget(
                pinned_items + other_items + character_items, STRUCTURED_BUDGET_TOKENS
            )
            worldview = "\n".join(worldview_lines)

            # -- section 2: current state ---------------------------------------
            state = await build_scene_state(uow, project_id, user_id)
            current_state = _format_scene_state(state)

            # -- lazy recap recompute (phase-2 item 8) --------------------------
            # Before the next chapter is written, every stale volume recap
            # gets a recompute job (committed with the retrieval_runs row
            # below, dispatched right after the uow closes). Until the
            # recompute lands the stale recap stays out of both this pack
            # and the RAG legs — never silently serving outdated memory.
            recompute_job_ids: list[str] = []
            if chapter_no is not None:
                try:
                    recompute_job_ids = await enqueue_stale_recap_recompute(
                        uow, project_id=project_id, chapter_no=chapter_no, user_id=user_id
                    )
                except Exception:
                    logger.warning("recap recompute enqueue failed project_id=%s", project_id, exc_info=True)
                    recompute_job_ids = []

            # -- section 3: constraints ------------------------------------------
            constraint_lines = list(constraints or [])
            if chapter_no is not None:
                constraint_lines.insert(0, f"当前章号：第{chapter_no}章")
            if query.strip():
                constraint_lines.append(f"本章目标：{query.strip()[:QUERY_MAX_CHARS]}")
            writing_constraints = "\n".join(f"- {line}" for line in constraint_lines)

            # -- section 4: evidence ---------------------------------------------
            evidence_lines: list[str] = []
            evidence_dropped: list[EvidenceBlock] = []
            used = 0
            for block in evidence:
                label = f"第{block.chapter_no}章" if block.chapter_no is not None else (block.document_title or "资料")
                line = f"【{label}】{block.content}"
                cost = _estimate_tokens(line)
                if used + cost > EVIDENCE_BUDGET_TOKENS:
                    evidence_dropped.append(block)
                    continue
                evidence_lines.append(line)
                used += cost
            evidence_text = "\n\n".join(evidence_lines)

            sections = {
                "worldview": worldview,
                "current_state": current_state,
                "constraints": writing_constraints,
                "evidence": evidence_text,
            }
            pack_text = render_pack_text(sections)
            token_cost = _estimate_tokens(pack_text)

            # -- retrieval_runs snapshot -----------------------------------------
            # Hit reasons (score/expanded), trim reasons (budget) and the
            # token budgets are all persisted for later inspection.
            selected_payload = {
                "chunks": [
                    {"chunk_id": block.chunk_id, "score": round(block.score, 6), "chapter_no": block.chapter_no, "expanded": block.expanded}
                    for block in evidence
                ],
                "trimmed": [
                    {"section": "worldview", "item": item[:200], "reason": "budget"}
                    for item in worldview_dropped
                ] + [
                    {"section": "evidence", "chunk_id": block.chunk_id, "chapter_no": block.chapter_no, "reason": "budget"}
                    for block in evidence_dropped
                ],
                "budget": {
                    "structured_tokens": STRUCTURED_BUDGET_TOKENS,
                    "evidence_tokens": EVIDENCE_BUDGET_TOKENS,
                    "token_cost": token_cost,
                },
            }
            if empty_index is not None:
                selected_payload["empty_index"] = empty_index
            run = RetrievalRunModel(
                id=new_id(),
                project_id=project_id,
                conversation_id=conversation_id,
                message_id=message_id,
                query_text=query[:QUERY_MAX_CHARS],
                intent=intent,
                filters_json=json.dumps({"chapter_no": chapter_no}, ensure_ascii=False),
                selected_chunks_json=json.dumps(selected_payload, ensure_ascii=False),
                elapsed_ms=(time.monotonic() - started) * 1000,
                token_cost=token_cost,
                created_at=datetime.now(UTC),
            )
            session.add(run)
            await uow.commit()
            pack = ScenePack(
                text=pack_text, sections=sections, evidence=evidence,
                run_id=run.id, token_cost=token_cost,
            )
        if recompute_job_ids:
            await self._dispatch_recap_recompute(recompute_job_ids, user_id)
        return pack

    async def _dispatch_recap_recompute(self, job_ids: list[str], user_id: str) -> None:
        """Best-effort immediate dispatch of lazy recompute jobs. Any
        failure leaves the rows pending — the job_type-routed sweeper
        redispatches them on its next pass."""
        try:
            from proseforge.infrastructure.tasks.factory import create_task_queue
            from proseforge.settings import get_settings

            queue = create_task_queue(get_settings(), self.session_factory)
            for job_id in job_ids:
                await queue.enqueue(
                    "proseforge.work.rollup_recap",
                    {"job_id": job_id, "user_id": user_id},
                )
        except Exception:
            logger.warning("recap recompute dispatch failed; jobs stay pending for the sweeper", exc_info=True)
