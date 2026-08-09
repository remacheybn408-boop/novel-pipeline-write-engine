"""swarm_entry TaskPlan assembly: validation and legacy fallback.

_task_plan_for_intent wraps the classified intent into a schema-validated
TaskPlan (epoch fixed at 1); any validation failure returns None so
handle_swarm_message falls back to the legacy graph_for_intent specs —
run-creation behavior must be identical on both paths.
"""

from __future__ import annotations

import pytest

from proseforge.application.agents import swarm_entry
from proseforge.application.agents.create_run import RunTaskSpec
from proseforge.application.agents.intent import graph_for_intent
from proseforge.domain.dispatch import TaskPlan

INTENTS = ("write", "review", "revise", "analyze")


def _legacy_specs(intent: str) -> list[RunTaskSpec]:
    return [
        RunTaskSpec(id=str(item["id"]), role=str(item["role"]), depends_on=tuple(item.get("depends_on", ())))
        for item in graph_for_intent(intent)
    ]


@pytest.mark.parametrize("intent", INTENTS)
def test_task_plan_built_for_every_graph_intent(intent: str) -> None:
    plan = swarm_entry._task_plan_for_intent(intent, project_id="proj-1", goal="写第三章")

    assert plan is not None
    assert plan.intent == intent
    assert plan.epoch == 1
    assert plan.scope.project_id == "proj-1"
    assert [task.id for task in plan.tasks] == [str(item["id"]) for item in graph_for_intent(intent)]


@pytest.mark.parametrize("intent", INTENTS)
def test_plan_specs_match_legacy_specs(intent: str) -> None:
    """The TaskPlan path must produce byte-identical RunTaskSpecs."""
    plan = swarm_entry._task_plan_for_intent(intent, project_id="proj-1", goal="写第三章")
    assert plan is not None

    plan_specs = [RunTaskSpec(id=task.id, role=task.role, depends_on=tuple(task.depends_on)) for task in plan.tasks]

    assert plan_specs == _legacy_specs(intent)


def test_validation_failure_returns_none_for_fallback(monkeypatch) -> None:
    def _broken_validate(payload):
        raise ValueError("schema exploded")

    monkeypatch.setattr(TaskPlan, "model_validate", staticmethod(_broken_validate))

    plan = swarm_entry._task_plan_for_intent("write", project_id="proj-1", goal="写第三章")

    assert plan is None
    # The legacy path still yields the full template — behavior unchanged.
    # (write 模板：14 节点管线（含 review_council 合议）+ promise_keeper 契约/核对/登记 3 节点)
    assert len(_legacy_specs("write")) == 17


def test_chat_intent_has_no_plan() -> None:
    # graph_for_intent raises ValueError for chat; the helper swallows it
    # into the fallback signal rather than leaking.
    assert swarm_entry._task_plan_for_intent("chat", project_id="proj-1", goal="你好") is None
