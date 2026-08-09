from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AgentRole(str, Enum):
    CHIEF_PLANNER = "chief_planner"
    STORY_ARCHITECT = "story_architect"
    WORLD_BUILDER = "world_builder"
    CHARACTER_DESIGNER = "character_designer"
    TIMELINE_ANALYST = "timeline_analyst"
    SCENE_WRITER = "scene_writer"
    STYLE_EDITOR = "style_editor"
    CONTINUITY_REVIEWER = "continuity_reviewer"
    ADVERSARIAL_REVIEWER = "adversarial_reviewer"
    MERGE_EDITOR = "merge_editor"
    CHIEF_EDITOR = "chief_editor"
    # Orchestrator's secretary: parses a dumped novel outline into a
    # per-chapter workflow. Follows the orchestrator model slot.
    ANALYST = "analyst"
    # Olivia the promise archivist: builds the per-chapter promise contract,
    # verifies fulfillments against the final draft and registers new
    # promises. Follows the orchestrator model slot.
    PROMISE_KEEPER = "promise_keeper"
    # Reserved placeholder slots (2026-08-05 decision): dormant sub-agent
    # seats for the write/review lanes. No graph references them yet —
    # graphs that do get valid default handling for free (handler fallback),
    # so future capabilities only need to fill in a dedicated handler.
    WRITE_RESERVE_1 = "write_reserve_1"
    WRITE_RESERVE_2 = "write_reserve_2"
    WRITE_RESERVE_3 = "write_reserve_3"
    REVIEW_RESERVE_1 = "review_reserve_1"
    REVIEW_RESERVE_2 = "review_reserve_2"
    REVIEW_RESERVE_3 = "review_reserve_3"

@dataclass(frozen=True)
class RolePolicy:
    role: AgentRole
    artifact_types: frozenset[str]
    tools: frozenset[str]
    max_tokens: int
    max_children: int
    can_activate_facts: bool = False
    can_create_revision: bool = False
    can_create_chapter_version: bool = False
    policy_version: str = "v3-policy-1"

CATALOG = {role: RolePolicy(role, frozenset({"report", "candidate"}), frozenset(), 12000, 4, can_create_revision=role is AgentRole.CHIEF_EDITOR) for role in AgentRole}
CATALOG[AgentRole.WORLD_BUILDER] = RolePolicy(AgentRole.WORLD_BUILDER, frozenset({"story_fact"}), frozenset(), 8000, 3)
