from __future__ import annotations

import pytest

from proseforge.application.story_bible.service import (
    StoryBibleService,
    StoryBibleStatusTransitionError,
)
from proseforge.domain.story_bible.entities import StoryFact, StoryFactValidationError


def _promise(status: str = "open") -> StoryFact:
    return StoryFact.create("project", "promise", "the letter", {"triggers": ["letter"], "budget_tokens": 20}, status=status)


def test_match_triggers_keeps_pins_and_only_matching_entries():
    seed = StoryFact.create("project", "location", "harbor", {"triggers": ["harbor"], "budget_tokens": 20})
    pinned = StoryFact(**{**seed.__dict__, "pinned": True})
    triggered = StoryFact.create("project", "character", "Mira", {"triggers": ["Mira"], "budget_tokens": 20})
    unmatched = StoryFact.create("project", "character", "Ilan", {"triggers": ["Ilan"], "budget_tokens": 20})

    matches = StoryBibleService.match_triggers([pinned, triggered, unmatched], "Mira enters the room")

    assert [(match.fact.id, match.reason) for match in matches] == [(pinned.id, "pinned"), (triggered.id, "trigger:Mira")]


def test_promise_rejects_terminal_state_transition_with_allowed_values():
    with pytest.raises(StoryBibleStatusTransitionError) as error:
        StoryBibleService.validate_status_transition(_promise("resolved"), "open")

    assert error.value.allowed == ()


def test_new_state_ledger_kinds_validate():
    chapter_fact = StoryFact.create("project", "chapter_fact", "ch3", {"timeline": "当夜", "chapter_no": 3})
    assert chapter_fact.status == "active"
    character_state = StoryFact.create("project", "character_state", "李雷", {"emotion": "焦虑", "mental": "失眠", "chapter_no": 3})
    assert character_state.status == "active"
    # The non-promise status rule still applies to the new kinds.
    with pytest.raises(StoryFactValidationError):
        StoryFact.create("project", "chapter_fact", "ch3", {}, status="open")


def test_voice_optional_dialect_and_catchphrases():
    base_voice = {
        "sentence_len": [5, 15],
        "connectors": ["然后"],
        "banned_words": ["非常"],
        "emotion_baseline": "外放",
        "register": "口语",
    }
    voice = {**base_voice, "dialect": "川普", "catchphrases": ["巴适", "要得"]}
    fact = StoryFact.create("project", "character", "李雷", {"voice": voice})
    assert fact.value["voice"]["dialect"] == "川普"
    assert fact.value["voice"]["catchphrases"] == ["巴适", "要得"]
    # The original five fields alone still validate (new fields optional).
    fact = StoryFact.create("project", "character", "李雷", {"voice": base_voice})
    assert "dialect" not in fact.value["voice"] and "catchphrases" not in fact.value["voice"]
    # Type checks: wrong shapes are rejected, unknown keys stay rejected.
    with pytest.raises(StoryFactValidationError):
        StoryFact.create("project", "character", "李雷", {"voice": {**base_voice, "dialect": 1}})
    with pytest.raises(StoryFactValidationError):
        StoryFact.create("project", "character", "李雷", {"voice": {**base_voice, "catchphrases": "巴适"}})
    with pytest.raises(StoryFactValidationError):
        StoryFact.create("project", "character", "李雷", {"voice": {**base_voice, "unknown": "x"}})
