from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChapterPlanInput:
    chapter_no: int
    volume_no: int
    title: str
    chapter_type: str
    goal: str
    main_event: str
    conflict: str
    characters: tuple[str, ...]
    plot_threads_to_advance: tuple[str, ...]
    canon_constraints: tuple[str, ...]
    ending_hook: str
    target_words: int


class PlanningService:
    def __init__(self, min_words: int = 500, max_words: int = 30_000):
        self.min_words = min_words
        self.max_words = max_words

    def validate(self, plans: list[ChapterPlanInput], *, expected_chapters: int, volumes: int, known_characters: set[str] | None = None, ending_direction: str = "") -> tuple[ChapterPlanInput, ...]:
        numbers = [item.chapter_no for item in plans]
        if sorted(numbers) != list(range(1, expected_chapters + 1)):
            raise ValueError("chapter numbers must cover every chapter exactly once")
        if any(item.volume_no < 1 or item.volume_no > volumes for item in plans):
            raise ValueError("chapter volume is outside the project volume range")
        if any(item.target_words < self.min_words or item.target_words > self.max_words for item in plans):
            raise ValueError("chapter target words outside policy")
        if known_characters:
            unknown = {name for item in plans for name in item.characters if name not in known_characters}
            if unknown:
                raise ValueError(f"unknown characters: {', '.join(sorted(unknown))}")
        if ending_direction and not any(ending_direction.lower() in item.ending_hook.lower() for item in plans):
            raise ValueError("required ending direction is not represented in plan")
        return tuple(plans)
