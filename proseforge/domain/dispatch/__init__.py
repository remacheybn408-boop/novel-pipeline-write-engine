"""Dispatch domain: structured task-plan protocol for the 五司调度 architecture."""

from proseforge.domain.dispatch.task_plan import (
    ALLOWED_ROLES,
    ChapterCard,
    ChapterCardCharacter,
    ChapterCardHookIn,
    ChapterCardHookOut,
    RagRequest,
    TaskPlan,
    TaskPlanIntent,
    TaskPlanScope,
    TaskSpec,
)

__all__ = [
    "ALLOWED_ROLES",
    "ChapterCard",
    "ChapterCardCharacter",
    "ChapterCardHookIn",
    "ChapterCardHookOut",
    "RagRequest",
    "TaskPlan",
    "TaskPlanIntent",
    "TaskPlanScope",
    "TaskSpec",
]
