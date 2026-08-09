from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChapterPlan:
    volume_no: int
    chapter_no: int
    title: str
    purpose: str
    word_target: int


def plan_chapters(*, volumes: int, chapters_per_volume: int, word_target: int, title_prefix: str = "Chapter") -> tuple[ChapterPlan, ...]:
    if volumes < 1 or chapters_per_volume < 1 or word_target < 1:
        raise ValueError("volumes, chapters_per_volume and word_target must be positive")
    return tuple(
        ChapterPlan(volume, (volume - 1) * chapters_per_volume + chapter, f"{title_prefix} {(volume - 1) * chapters_per_volume + chapter}", "advance the central conflict", word_target)
        for volume in range(1, volumes + 1) for chapter in range(1, chapters_per_volume + 1)
    )
