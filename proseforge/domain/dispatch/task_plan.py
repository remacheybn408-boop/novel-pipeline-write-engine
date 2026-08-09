"""Dispatch task-plan protocol (五司调度 structured task cards).

TaskPlan replaces free-text handoff between the orchestrator and run
creation. Safety boundary (application/agents/expand_graph.py): model
output never creates tasks directly — a TaskPlan may only reference the
existing graph templates (application/agents/intent.graph_for_intent) and
registered roles (domain/agents/roles.AgentRole), and only becomes tasks
after schema validation. Callers must fall back to the legacy rule path
when validation fails. ``epoch`` is validated and passed through only;
no expiry rejection at this stage.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from proseforge.domain.agents.roles import AgentRole

# Intents backed by a graph template in graph_for_intent ("chat" never
# creates a run, so it is not a valid TaskPlan intent).
TaskPlanIntent = Literal["write", "review", "revise", "analyze"]

ALLOWED_ROLES: frozenset[str] = frozenset(role.value for role in AgentRole)


class TaskSpec(BaseModel):
    """One task inside a TaskPlan; mirrors the graph-template node shape."""

    id: str
    role: str
    chapters: str | None = None
    goal: str
    rag: list[str] | None = None
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("role")
    @classmethod
    def _role_must_be_registered(cls, value: str) -> str:
        if value not in ALLOWED_ROLES:
            raise ValueError(f"role not registered: {value}")
        return value


class TaskPlanScope(BaseModel):
    project_id: str
    chapters: str | None = None


class TaskPlan(BaseModel):
    """Structured task card assembled from a classified intent."""

    intent: TaskPlanIntent
    scope: TaskPlanScope
    global_notes: str | None = None
    tasks: list[TaskSpec] = Field(min_length=1)
    epoch: int = Field(default=1, ge=1)
    capabilities: dict[str, bool] | None = None

    @model_validator(mode="after")
    def _depends_on_must_form_dag(self) -> TaskPlan:
        ids = {task.id for task in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("duplicate task id in tasks")
        for task in self.tasks:
            for dependency in task.depends_on:
                if dependency not in ids:
                    raise ValueError(f"depends_on references undefined task id: {dependency}")
        # Cycle check: DFS over the dependency graph; a node revisited on
        # the current stack means a cycle.
        graph = {task.id: task.depends_on for task in self.tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def _walk(node: str) -> None:
            if node in visited:
                return
            if node in visiting:
                raise ValueError(f"dependency cycle detected at task: {node}")
            visiting.add(node)
            for dependency in graph[node]:
                _walk(dependency)
            visiting.discard(node)
            visited.add(node)

        for task_id in graph:
            _walk(task_id)
        return self


# ---------------------------------------------------------------------------
# Chapter card: the analyst's per-chapter structured handoff. Field names
# are the canonical contract; the analyst prompt's current JSON keys
# (chapter_no/title/summary/hooks) map onto these (see agents/prompts.py).
# ---------------------------------------------------------------------------


class ChapterCardCharacter(BaseModel):
    name: str
    state: str


class ChapterCardHookIn(BaseModel):
    type: str
    desc: str


class ChapterCardHookOut(BaseModel):
    hook_id: str
    resolved: bool


class ChapterCard(BaseModel):
    chapter: int
    summary: str
    characters: list[ChapterCardCharacter] = Field(default_factory=list)
    timeline: str | None = None
    hooks_in: list[ChapterCardHookIn] = Field(default_factory=list)
    hooks_out: list[ChapterCardHookOut] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class RagRequest(BaseModel):
    """Role-initiated retrieval request; type discriminator is fixed."""

    type: Literal["rag_request"] = "rag_request"
    from_role: str
    project_id: str
    query: str
    collections: list[str]
    top_k: int = 12
