"""Scene-state assembly (phase-3 scene-pack skeleton, exposed as an API).

Builds the project's current scene block: latest chapter, the last five
chapter summaries, characters appearing in the latest chapter (alias
matcher), pinned story-bible entries, open promises, the latest per-
character emotional/mental states and the last 1-2 chapter_fact snapshots
(auto-extracted by the chapter summarizer). Memory-pyramid recaps
(volume/book/era) join the block too — STALE recaps never do (phase-2
item 8: a revised chapter invalidates its covering recaps in the same
commit, and invalidated recaps stay out of the context until recomputed).
A rough character budget keeps the block small: recaps are dropped first
(derived content, also reachable via RAG), then chapter facts and
character states, then recent summaries oldest-first, then story entries,
then the latest summary is hard-truncated.
"""

from __future__ import annotations

import json
import re

from sqlalchemy import select

from proseforge.domain.characters.matching import match_characters
from proseforge.domain.story_bible.entities import RETRIEVABLE_STATUSES
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.recap import RecapRollupModel
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel

CHAR_BUDGET = 4000
RECENT_SUMMARY_COUNT = 5
# State-ledger cap: only the newest chapter_fact snapshots are injected.
RECENT_CHAPTER_FACT_COUNT = 2

_CHAPTER_FACT_KEY_PATTERN = re.compile(r"^ch(\d+)$")


def _character_block(character) -> dict[str, object]:
    return {
        "id": character.id,
        "name": character.name,
        "aliases": character.aliases,
        "summary": character.summary,
        "role": character.role,
        "source": character.source,
    }


async def build_scene_state(uow, project_id: str, owner_id: str) -> dict[str, object]:
    session = uow.session
    chapters = list((await session.scalars(
        select(ChapterModel).where(ChapterModel.project_id == project_id).order_by(ChapterModel.chapter_no)
    )).all())
    version_ids = [chapter.active_version_id for chapter in chapters if chapter.active_version_id]
    versions: dict[str, ChapterVersionModel] = {}
    if version_ids:
        rows = await session.scalars(select(ChapterVersionModel).where(ChapterVersionModel.id.in_(version_ids)))
        versions = {row.id: row for row in rows}

    active = [(chapter, versions[chapter.active_version_id]) for chapter in chapters if chapter.active_version_id in versions]
    latest_chapter: dict[str, object] | None = None
    latest_content = ""
    if active:
        chapter, version = active[-1]
        latest_chapter = {"no": chapter.chapter_no, "title": chapter.title, "summary": version.summary}
        latest_content = version.content

    recent = [
        {"no": chapter.chapter_no, "title": chapter.title, "summary": version.summary}
        for chapter, version in reversed(active[-RECENT_SUMMARY_COUNT:])
    ]

    characters = [
        _character_block(character)
        for character in match_characters(latest_content, await uow.characters.list_owned(project_id, owner_id))
    ] if latest_content else []

    story_rows = (await session.scalars(
        select(StoryBibleEntryModel).where(
            StoryBibleEntryModel.project_id == project_id,
            # Guard: excluded rows and terminal promise states stay out.
            StoryBibleEntryModel.status.in_(RETRIEVABLE_STATUSES),
        ).order_by(StoryBibleEntryModel.kind, StoryBibleEntryModel.key)
    )).all()
    pinned = [
        {"kind": row.kind, "key": row.key, "value": json.loads(row.value_json or "{}")}
        for row in story_rows if row.pinned
    ]
    promises = [
        {"kind": row.kind, "key": row.key, "value": json.loads(row.value_json or "{}"), "status": row.status}
        for row in story_rows if row.kind == "promise" and row.status == "open"
    ]
    character_states = [
        {"key": row.key, "value": json.loads(row.value_json or "{}")}
        for row in story_rows if row.kind == "character_state"
    ]
    chapter_facts = sorted(
        (
            {"chapter_no": _chapter_fact_no(row.key), "value": json.loads(row.value_json or "{}")}
            for row in story_rows if row.kind == "chapter_fact"
        ),
        key=lambda item: item["chapter_no"], reverse=True,
    )[:RECENT_CHAPTER_FACT_COUNT]

    # Memory pyramid: only settled (non-stale) recaps enter the context.
    # A stale row means a covered chapter was revised after the recap was
    # compressed; injecting it would feed outdated memory to the writer.
    recap_rows = (await session.scalars(
        select(RecapRollupModel).where(
            RecapRollupModel.project_id == project_id,
            RecapRollupModel.stale.is_(False),
            RecapRollupModel.content != "",
        ).order_by(RecapRollupModel.span_start)
    )).all()
    recaps = [
        {"level": row.level, "span_start": row.span_start, "span_end": row.span_end, "content": row.content}
        for row in recap_rows
    ]

    state: dict[str, object] = {
        "project_id": project_id,
        "latest_chapter": latest_chapter,
        "recent_summaries": recent,
        "characters": characters,
        "pinned_facts": pinned,
        "open_promises": promises,
        "character_states": character_states,
        "chapter_facts": chapter_facts,
        "recaps": recaps,
    }
    _enforce_budget(state)
    return state


def _chapter_fact_no(key: str) -> int:
    match = _CHAPTER_FACT_KEY_PATTERN.match(key)
    return int(match.group(1)) if match else 0


def _text_size(state: dict[str, object]) -> int:
    size = 0
    latest = state.get("latest_chapter") or {}
    size += len(str(latest.get("summary", "")))
    size += sum(len(str(item.get("summary", ""))) for item in state["recent_summaries"])
    size += sum(len(str(item.get("summary", ""))) for item in state["characters"])
    size += sum(len(str(item.get("content", ""))) for item in state.get("recaps", []))
    size += sum(len(json.dumps(item.get("value", {}), ensure_ascii=False)) for item in state["pinned_facts"])
    size += sum(len(json.dumps(item.get("value", {}), ensure_ascii=False)) for item in state["open_promises"])
    size += sum(len(json.dumps(item.get("value", {}), ensure_ascii=False)) for item in state.get("character_states", []))
    size += sum(len(json.dumps(item.get("value", {}), ensure_ascii=False)) for item in state.get("chapter_facts", []))
    return size


def _enforce_budget(state: dict[str, object]) -> None:
    # Rough budget: derived recaps go first (their content is distilled
    # from the summaries below and reachable via RAG), then the auto-
    # extracted ledger blocks (chapter facts oldest-first, then character
    # states), then oldest recent summaries, then oldest pinned facts, and
    # finally the latest summary is hard-truncated.
    while state.get("recaps") and _text_size(state) > CHAR_BUDGET:
        state["recaps"].pop()
    while state.get("chapter_facts") and _text_size(state) > CHAR_BUDGET:
        state["chapter_facts"].pop()  # oldest (list is newest-first)
    while state.get("character_states") and _text_size(state) > CHAR_BUDGET:
        state["character_states"].pop()
    while len(state["recent_summaries"]) > 1 and _text_size(state) > CHAR_BUDGET:
        state["recent_summaries"].pop()  # oldest (list is newest-first)
    while state["pinned_facts"] and _text_size(state) > CHAR_BUDGET:
        state["pinned_facts"].pop()
    if _text_size(state) > CHAR_BUDGET and state.get("latest_chapter"):
        latest = state["latest_chapter"]
        latest["summary"] = str(latest.get("summary", ""))[:CHAR_BUDGET]
