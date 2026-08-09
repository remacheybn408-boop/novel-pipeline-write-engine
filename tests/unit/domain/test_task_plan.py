"""TaskPlan / ChapterCard / RagRequest schema tests (domain/dispatch).

Covers the happy path, unregistered roles, depends_on referencing undefined
ids, dependency-cycle detection, and epoch default/bounds.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from proseforge.domain.dispatch import ChapterCard, RagRequest, TaskPlan


def _valid_payload() -> dict:
    return {
        "intent": "write",
        "scope": {"project_id": "proj-1", "chapters": "3"},
        "tasks": [
            {"id": "planner", "role": "chief_planner", "goal": "写第三章", "depends_on": []},
            {"id": "scene_a", "role": "scene_writer", "goal": "写第三章", "depends_on": ["planner"], "rag": ["chapters"]},
        ],
    }


def test_valid_task_plan_passes() -> None:
    plan = TaskPlan.model_validate(_valid_payload())

    assert plan.intent == "write"
    assert plan.scope.project_id == "proj-1"
    assert [task.id for task in plan.tasks] == ["planner", "scene_a"]
    assert plan.tasks[1].depends_on == ["planner"]
    assert plan.tasks[1].rag == ["chapters"]


def test_epoch_defaults_to_one() -> None:
    plan = TaskPlan.model_validate(_valid_payload())

    assert plan.epoch == 1


def test_epoch_must_be_at_least_one() -> None:
    payload = {**_valid_payload(), "epoch": 0}

    with pytest.raises(ValidationError):
        TaskPlan.model_validate(payload)


def test_unknown_intent_rejected() -> None:
    payload = {**_valid_payload(), "intent": "chat"}

    with pytest.raises(ValidationError):
        TaskPlan.model_validate(payload)


def test_unregistered_role_rejected() -> None:
    payload = _valid_payload()
    payload["tasks"][0]["role"] = "ghost_writer"

    with pytest.raises(ValidationError, match="role not registered"):
        TaskPlan.model_validate(payload)


def test_depends_on_undefined_id_rejected() -> None:
    payload = _valid_payload()
    payload["tasks"][1]["depends_on"] = ["missing"]

    with pytest.raises(ValidationError, match="undefined task id"):
        TaskPlan.model_validate(payload)


def test_dependency_cycle_rejected() -> None:
    payload = _valid_payload()
    payload["tasks"][0]["depends_on"] = ["scene_a"]  # planner <-> scene_a

    with pytest.raises(ValidationError, match="cycle"):
        TaskPlan.model_validate(payload)


def test_self_dependency_rejected_as_cycle() -> None:
    payload = _valid_payload()
    payload["tasks"][0]["depends_on"] = ["planner"]

    with pytest.raises(ValidationError, match="cycle"):
        TaskPlan.model_validate(payload)


def test_duplicate_task_id_rejected() -> None:
    payload = _valid_payload()
    payload["tasks"].append(dict(payload["tasks"][0]))

    with pytest.raises(ValidationError, match="duplicate task id"):
        TaskPlan.model_validate(payload)


def test_empty_tasks_rejected() -> None:
    payload = {**_valid_payload(), "tasks": []}

    with pytest.raises(ValidationError):
        TaskPlan.model_validate(payload)


def test_chapter_card_shape() -> None:
    card = ChapterCard.model_validate({
        "chapter": 3,
        "summary": "雨夜对峙",
        "characters": [{"name": "林风", "state": "受伤"}],
        "timeline": "第三夜",
        "hooks_in": [{"type": "foreshadow", "desc": "旧信物"}],
        "hooks_out": [{"hook_id": "h-1", "resolved": False}],
        "issues": ["时间线存疑"],
    })

    assert card.chapter == 3
    assert card.characters[0].name == "林风"
    assert card.hooks_out[0].resolved is False


def test_chapter_card_optional_fields_default() -> None:
    card = ChapterCard.model_validate({"chapter": 1, "summary": "开篇"})

    assert card.characters == [] and card.timeline is None
    assert card.hooks_in == [] and card.hooks_out == [] and card.issues == []


def test_rag_request_shape_and_defaults() -> None:
    request = RagRequest.model_validate({
        "type": "rag_request",
        "from_role": "scene_writer",
        "project_id": "proj-1",
        "query": "雨夜 场景",
        "collections": ["chapters", "story_bible"],
    })

    assert request.type == "rag_request"
    assert request.top_k == 12


def test_rag_request_rejects_other_type() -> None:
    with pytest.raises(ValidationError):
        RagRequest.model_validate({
            "type": "rag_reply",
            "from_role": "scene_writer",
            "project_id": "proj-1",
            "query": "雨夜",
            "collections": [],
        })
