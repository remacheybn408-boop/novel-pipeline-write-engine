from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from proseforge.application.story_bible.service import StoryBibleService
from proseforge.context_engine.tokenizer import ConservativeTokenizer
from proseforge.domain.common.ids import new_id
from proseforge.domain.story_bible.entities import StoryFact
from proseforge.infrastructure.database.models.remaining import ContextSnapshotModel


@dataclass(frozen=True)
class SnapshotSelection:
    blocks: tuple[dict[str, Any], ...]
    injected_fact_ids: tuple[str, ...]
    omitted: tuple[dict[str, str], ...]


class BuildContextSnapshot:
    """Select and persist an auditable, deterministic generation context."""

    def __init__(self, tokenizer: ConservativeTokenizer | None = None):
        self.tokenizer = tokenizer or ConservativeTokenizer()

    def select_story_facts(self, entries: Iterable[Any], matching_text: str, token_budget: int) -> SnapshotSelection:
        facts = [self._fact_from_row(row) for row in entries]
        matches = StoryBibleService.match_triggers(facts, matching_text)
        matched = {hit.fact.id: hit for hit in matches}
        omitted: list[dict[str, str]] = [
            {"source_type": "story_bible", "source_id": fact.id, "reason": "not_triggered"}
            for fact in facts if fact.id not in matched
        ]
        used = 0
        blocks: list[dict[str, Any]] = []
        for hit in sorted(matches, key=lambda item: (not item.fact.pinned, -self._confidence(item.fact), item.fact.kind, item.fact.key, item.fact.id)):
            block = self._fact_block(hit.fact, hit.reason)
            estimate = int(block["token_estimate"])
            if not hit.fact.pinned and used + estimate > token_budget:
                omitted.append({"source_type": "story_bible", "source_id": hit.fact.id, "reason": "budget_exceeded"})
                continue
            blocks.append(block)
            used += estimate
        return SnapshotSelection(tuple(blocks), tuple(str(block["source_id"]) for block in blocks), tuple(omitted))

    def persist(self, session, *, project_id: str, blocks: list[dict[str, Any]], messages, injected_fact_ids: Iterable[str], omitted: Iterable[dict[str, str]], budget) -> ContextSnapshotModel:
        payload = {
            "blocks": blocks,
            "message_ids": [message.id for message in messages],
            "injected_fact_ids": list(injected_fact_ids),
            "injected_fact_reasons": {str(block["source_id"]): str(block.get("reason", "")) for block in blocks if block.get("source_type") == "story_bible"},
            "omitted": list(omitted),
            "budget": {"context_window": budget.context_window, "input_tokens": budget.input_tokens, "output_reserve": budget.output_reserve},
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        snapshot = ContextSnapshotModel(id=new_id(), project_id=project_id, snapshot_hash=hashlib.sha256(encoded.encode()).hexdigest(), payload=encoded)
        session.add(snapshot)
        return snapshot

    def describe_block(self, *, block_type: str, source_id: str, text: str, priority: int = 0, pinned: bool = False, **extra: Any) -> dict[str, Any]:
        return {"type": block_type, "source_type": block_type, "source_id": source_id, "text": text, "token_estimate": self.tokenizer.count(text), "priority": priority, "pinned": pinned, "redaction": False, **extra}

    def _fact_block(self, fact: StoryFact, reason: str) -> dict[str, Any]:
        text = f"[{fact.kind}] {fact.key}: {json.dumps(fact.value, ensure_ascii=False, sort_keys=True)}"
        configured_budget = fact.value.get("budget_tokens", 0)
        estimate = max(self.tokenizer.count(text), int(configured_budget) if isinstance(configured_budget, int) else 0)
        return self.describe_block(block_type="story_fact", source_id=fact.id, text=text, priority=100 if fact.pinned else int(self._confidence(fact) * 100), pinned=fact.pinned, fact_id=fact.id, reason=reason, token_estimate=estimate)

    @staticmethod
    def _confidence(fact: StoryFact) -> float:
        return fact.confidence

    @staticmethod
    def _fact_from_row(row: Any) -> StoryFact:
        return StoryBibleService.from_record(row)
