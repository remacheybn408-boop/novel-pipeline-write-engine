"""Promise lifecycle sync from write-run goals.

Batch write-run goals carry a deterministic 「伏笔/钩子：X」 line
(batch_dispatch.chapter_goal): directive prefixes 埋入/铺垫/暗示 plant a
promise (status=open, introduced_chapter recorded), 回收/照应 resolve the
same-key promise. Single-chapter goals have no such line and the sync is a
no-op. All transitions honour PROMISE_TRANSITIONS (an open promise passes
through developing on its way to resolved). Re-running the same chapter
never creates duplicate rows: a repeated plant on an open promise only
pushes it to developing.

Called best-effort from writeback_chapter; failures are logged by the
caller and never block the chapter writeback.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from sqlalchemy import select

from proseforge.domain.story_bible.entities import PROMISE_TRANSITIONS, StoryFact
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel

logger = logging.getLogger(__name__)

_HOOKS_LINE_PATTERN = re.compile(r"伏笔/钩子[：:]([^\n]+)")
_PLANT_PREFIXES = ("埋入", "铺垫", "暗示")
_RESOLVE_PREFIXES = ("回收", "照应")
_PREFIX_PATTERN = re.compile(r"^(埋入|铺垫|暗示|回收|照应)[：:]?")
_SEGMENT_SPLIT_PATTERN = re.compile(r"[，,；;、]")


def parse_goal_hooks(goal_text: str) -> list[tuple[str, str]]:
    """Parse the goal's 「伏笔/钩子：X」 line into (action, clue) pairs.

    action is "plant" (埋入/铺垫/暗示) or "resolve" (回收/照应). Segments
    without a prefix inherit the previous segment's action, so both
    「埋入：A；回收：B」 and 「埋入A，B」 parse. Returns [] when the line is
    absent (single-chapter goals).
    """
    match = _HOOKS_LINE_PATTERN.search(goal_text or "")
    if not match:
        return []
    actions: list[tuple[str, str]] = []
    current: str | None = None
    for raw_segment in _SEGMENT_SPLIT_PATTERN.split(match.group(1)):
        segment = raw_segment.strip()
        if not segment:
            continue
        prefix = _PREFIX_PATTERN.match(segment)
        if prefix:
            current = "plant" if prefix.group(1) in _PLANT_PREFIXES else "resolve"
            segment = segment[prefix.end():].strip()
        if not segment or current is None:
            continue
        pair = (current, segment)
        if pair not in actions:
            actions.append(pair)
    return actions


def _apply_transition(row: StoryBibleEntryModel, target: str, now: datetime, *, extra: dict[str, object] | None = None) -> None:
    allowed = PROMISE_TRANSITIONS.get(row.status, ())
    if target not in allowed:
        raise ValueError(f"invalid promise transition {row.status} -> {target}")
    try:
        value = json.loads(row.value_json or "{}")
    except ValueError:
        value = {}
    if extra:
        value.update(extra)
    row.value_json = json.dumps(value, ensure_ascii=False)
    row.status = target
    row.version = int(row.version) + 1
    row.updated_at = now


async def sync_promises_from_goal(session, *, project_id: str, chapter_no: int, goal_text: str) -> dict[str, int]:
    """Upsert/transition promise entries from the goal's hooks line.

    Returns counters {"planted", "developed", "resolved"}; all zeros when
    the goal carries no 「伏笔/钩子：」 line.
    """
    actions = parse_goal_hooks(goal_text)
    counts = {"planted": 0, "developed": 0, "resolved": 0}
    if not actions:
        return counts
    rows = list((await session.scalars(
        select(StoryBibleEntryModel).where(
            StoryBibleEntryModel.project_id == project_id,
            StoryBibleEntryModel.kind == "promise",
            StoryBibleEntryModel.key.in_([clue for _action, clue in actions]),
        )
    )).all())
    by_key = {row.key: row for row in rows}
    now = datetime.now(UTC)
    for action, clue in actions:
        row = by_key.get(clue)
        if action == "plant":
            if row is None:
                fact = StoryFact.create(
                    project_id, "promise", clue,
                    {"note": clue, "introduced_chapter": chapter_no},
                    source="auto",
                )
                new_row = StoryBibleEntryModel(
                    id=fact.id, project_id=project_id, kind="promise", key=clue,
                    value_json=json.dumps(fact.value, ensure_ascii=False),
                    status="open", confidence=1.0, source="auto", pinned=False,
                    version=1, created_at=now, updated_at=now,
                )
                session.add(new_row)
                by_key[clue] = new_row
                counts["planted"] += 1
            elif row.status == "open":
                # Repeated plant of an open promise: push the lifecycle
                # forward instead of duplicating the row.
                _apply_transition(row, "developing", now)
                counts["developed"] += 1
            # developing/resolved/abandoned/excluded rows are left alone
            # (idempotent on reruns).
        else:  # resolve
            if row is None or row.status not in ("open", "developing"):
                continue
            if row.status == "open":
                # PROMISE_TRANSITIONS: open -> developing -> resolved.
                _apply_transition(row, "developing", now)
            _apply_transition(row, "resolved", now, extra={"resolved_chapter": chapter_no})
            counts["resolved"] += 1
    return counts
