"""Canon conflict detection v1 (heuristic).

Rule: same entity + same canonical field + different normalized value ->
a canon_conflicts row (evidence only — NOTHING is auto-overwritten; the
LLM contradiction judge is phase 4). Facts come from the chapter
summarizer's extended JSON. Entities are matched against character
name/aliases and story-bible keys with case/whitespace/punctuation
normalization; field names are mapped to canonical keys; only short
values (<=50 chars) are compared — prose is never a conflict.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

from proseforge.domain.story_bible.entities import RETRIEVABLE_STATUSES

MAX_FACT_VALUE_CHARS = 50

# Canonical field keys -> accepted surface names (normalized).
FIELD_ALIASES: dict[str, set[str]] = {
    "role": {"角色", "身份", "定位", "角色定位", "身份定位", "戏份", "role"},
    "alias": {"别名", "外号", "称呼", "昵称", "alias"},
    "status": {"状态", "生死", "status"},
    "location": {"地点", "位置", "所在地", "居所", "location"},
    "relationship": {"关系", "人物关系", "relationship"},
}

_PUNCT_RE = re.compile(r"[\s　,，.。、;；:：!！?？·・\-—_'‘’“”\"'()（）\[\]【】]+")


@dataclass(frozen=True)
class ConflictCandidate:
    entity: str
    field: str
    candidate_value: str
    existing_value: str
    conflicting_source: str  # "character:{id}" | "story_bible:{id}"
    claim: str  # human-readable field_or_claim


def normalize_text(value: str) -> str:
    """Case/whitespace/punctuation-insensitive normalization (NFKC first
    so full-width variants fold)."""
    folded = unicodedata.normalize("NFKC", str(value)).lower()
    return _PUNCT_RE.sub("", folded)


def canonical_field(field: str) -> str | None:
    needle = normalize_text(field)
    if not needle:
        return None
    for canonical, names in FIELD_ALIASES.items():
        if needle in {normalize_text(name) for name in names}:
            return canonical
    return None


def _comparable(value: str) -> bool:
    return 0 < len(str(value).strip()) <= MAX_FACT_VALUE_CHARS


def find_conflicts(
    facts: list[dict[str, object]],
    characters: list,
    story_bible_rows: list,
) -> list[ConflictCandidate]:
    """Pure comparison: facts x (characters.role, story_bible value_json fields)."""
    character_by_name: dict[str, object] = {}
    for character in characters:
        for term in [character.name, *character.aliases]:
            character_by_name.setdefault(normalize_text(term), character)
    story_by_key: dict[str, object] = {}
    for row in story_bible_rows:
        story_by_key.setdefault(normalize_text(row.key), row)

    conflicts: list[ConflictCandidate] = []
    seen: set[tuple[str, str, str]] = set()
    for fact in facts:
        entity = str(fact.get("entity", "")).strip()
        field = canonical_field(str(fact.get("field", "")))
        value = str(fact.get("value", "")).strip()
        if not entity or field is None or not _comparable(value):
            continue
        entity_key = normalize_text(entity)

        character = character_by_name.get(entity_key)
        if character is not None and field == "role" and _comparable(character.role) and normalize_text(character.role) != normalize_text(value):
            key = (f"character:{character.id}", field, normalize_text(value))
            if key not in seen:
                seen.add(key)
                conflicts.append(ConflictCandidate(
                    entity=entity, field=field, candidate_value=value,
                    existing_value=character.role,
                    conflicting_source=f"character:{character.id}",
                    claim=f"{character.name} 的 {field}（角色定位）",
                ))

        story_row = story_by_key.get(entity_key)
        if story_row is not None:
            try:
                stored = json.loads(story_row.value_json or "{}")
            except ValueError:
                stored = {}
            existing = None
            if isinstance(stored, dict):
                for raw_key, raw_value in stored.items():
                    if canonical_field(str(raw_key)) == field and isinstance(raw_value, str):
                        existing = raw_value
                        break
            if existing and _comparable(existing) and normalize_text(existing) != normalize_text(value):
                key = (f"story_bible:{story_row.id}", field, normalize_text(value))
                if key not in seen:
                    seen.add(key)
                    conflicts.append(ConflictCandidate(
                        entity=entity, field=field, candidate_value=value,
                        existing_value=existing,
                        conflicting_source=f"story_bible:{story_row.id}",
                        claim=f"{story_row.key} 的 {field}",
                    ))
    return conflicts


async def check_conflicts(uow, *, project_id: str, version_id: str, chapter_no: int, facts: list[dict[str, object]]) -> int:
    """Persist conflict rows for extracted facts; returns rows written.

    De-dupes against existing OPEN rows with the same
    candidate+conflicting+field. Never raises on bad fact shapes.
    """
    if not facts:
        return 0
    from sqlalchemy import select

    from proseforge.infrastructure.database.models.story_bible import (
        StoryBibleEntryModel,
    )

    characters = await uow.characters.list_for_project(project_id)
    story_rows = list((await uow.session.scalars(
        select(StoryBibleEntryModel).where(
            StoryBibleEntryModel.project_id == project_id,
            # Guard: excluded/terminal rows are not canon evidence either.
            StoryBibleEntryModel.status.in_(RETRIEVABLE_STATUSES),
        )
    )).all())
    candidates = find_conflicts(facts, characters, story_rows)
    written = 0
    for candidate in candidates:
        created = await uow.retrieval.add_conflict_if_open_absent(
            project_id=project_id,
            candidate_source=f"chapter_version:{version_id}",
            conflicting_source=candidate.conflicting_source,
            field_or_claim=candidate.claim,
            evidence={
                "candidate_value": candidate.candidate_value,
                "existing_value": candidate.existing_value,
                "chapter_no": chapter_no,
            },
        )
        written += int(created)
    return written
