from __future__ import annotations

from proseforge.domain.chapter.entity import Chapter
from proseforge.domain.project.entity import Project

from .scanner import LegacySlot
from .sqlite_reader import LegacySnapshot


def map_project(slot: LegacySlot) -> Project:
    data = slot.project
    return Project.create(owner_id=str(data.get("owner_id", "legacy")), slug=slot.root.name, title=str(data.get("title", data.get("name", slot.root.name))))


def map_chapters(project: Project, snapshot: LegacySnapshot) -> tuple[Chapter, ...]:
    return tuple(Chapter.create(project_id=project.id, chapter_no=item.chapter_no, title=item.title) for item in snapshot.chapters)
