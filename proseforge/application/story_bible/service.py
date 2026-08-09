from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from proseforge.domain.story_bible.entities import (
    PROMISE_TRANSITIONS,
    StoryFact,
    StoryFactValidationError,
)


class StoryBibleStatusTransitionError(ValueError):
    def __init__(self, *, allowed: tuple[str, ...]):
        self.allowed = allowed
        super().__init__("invalid story bible status transition")


@dataclass(frozen=True)
class TriggeredFact:
    fact: StoryFact
    reason: str


class StoryBibleService:
    """Domain operations shared by the Story Bible HTTP endpoints and context builder."""

    @staticmethod
    def from_record(record) -> StoryFact:
        return StoryFact(
            id=record.id,
            project_id=record.project_id,
            kind=record.kind,
            key=record.key,
            value=json.loads(record.value_json),
            pinned=record.pinned,
            status=record.status,
            version=record.version,
            confidence=record.confidence,
            source=record.source,
        )

    @staticmethod
    def update_fact(
        fact: StoryFact,
        changes: Mapping[str, object],
    ) -> StoryFact:
        unknown = set(changes) - {"kind", "key", "value", "pinned"}
        if unknown:
            raise StoryFactValidationError(f"unsupported fact fields: {', '.join(sorted(unknown))}")
        if not changes:
            raise StoryFactValidationError("at least one fact field must be supplied")

        kind = changes.get("kind", fact.kind)
        key = changes.get("key", fact.key)
        value = changes.get("value", fact.value)
        pinned = changes.get("pinned", fact.pinned)
        if not isinstance(kind, str):
            raise StoryFactValidationError("kind must be a string")
        if not isinstance(key, str):
            raise StoryFactValidationError("key must be a string")
        if not isinstance(value, Mapping):
            raise StoryFactValidationError("value must be an object")
        if type(pinned) is not bool:
            raise StoryFactValidationError("pinned must be a boolean")

        status = fact.status
        if kind != fact.kind:
            status = "open" if kind == "promise" else "active"
        return StoryFact(
            id=fact.id,
            project_id=fact.project_id,
            kind=kind,
            key=key,
            value=dict(value),
            pinned=pinned,
            status=status,
            version=fact.version,
            confidence=fact.confidence,
            source=fact.source,
        )

    @staticmethod
    def validate_status_transition(fact: StoryFact, target_status: str) -> StoryFact:
        if fact.kind != "promise":
            raise StoryBibleStatusTransitionError(allowed=())
        allowed = PROMISE_TRANSITIONS[fact.status]
        if target_status not in allowed:
            raise StoryBibleStatusTransitionError(allowed=allowed)
        return replace(fact, status=target_status)

    @staticmethod
    def match_triggers(entries: Iterable[StoryFact], text: str) -> list[TriggeredFact]:
        matches: list[TriggeredFact] = []
        for entry in entries:
            triggers = entry.value.get("triggers") or [entry.key]
            matched = [trigger for trigger in triggers if isinstance(trigger, str) and trigger in text]
            if entry.pinned:
                matches.append(TriggeredFact(entry, reason="pinned"))
            elif matched:
                matches.append(TriggeredFact(entry, reason=f"trigger:{matched[0]}"))
        return matches
