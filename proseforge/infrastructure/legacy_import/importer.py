from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

from .mapper import map_chapters, map_project
from .scanner import scan_workspace
from .sqlite_reader import read_legacy_slot


@dataclass(frozen=True)
class LegacyImportReport:
    status: str
    projects_imported: int
    chapters_imported: int
    hash_mismatches: tuple[str, ...] = ()
    failed_slots: tuple[str, ...] = ()


class LegacyImporter:
    def __init__(self, archive_root: str | Path = "/data/backups/legacy-import", session_factory=None, owner_id: str | None = None):
        self.archive_root = Path(archive_root)
        self.session_factory = session_factory
        self.owner_id = owner_id

    async def import_workspace(self, workspace: str | Path) -> LegacyImportReport:
        imported_projects = imported_chapters = 0
        mismatches: list[str] = []
        failures: list[str] = []
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        for slot in scan_workspace(workspace):
            try:
                snapshot = read_legacy_slot(slot.root, slot.database)
                project = map_project(slot)
                if self.owner_id:
                    project = Project.create(owner_id=self.owner_id, slug=project.slug, title=project.title, genre=project.genre, style=project.style)
                chapters = map_chapters(project, snapshot)
                if self.session_factory:
                    async with SqlAlchemyUnitOfWork(self.session_factory) as uow:
                        existing = await uow.projects.get_by_slug(project.owner_id, project.slug)
                        if existing is None:
                            await uow.projects.add(project)
                            for legacy_chapter, chapter in zip(snapshot.chapters, chapters, strict=True):
                                await uow.chapters.add(chapter)
                                for content in legacy_chapter.versions:
                                    version = await uow.chapters.append_version(chapter_id=chapter.id, content=content)
                                    await uow.chapters.set_active_version(chapter.id, version.id)
                            await uow.commit()
                        else:
                            project = existing
                imported_projects += 1
                imported_chapters += len(snapshot.chapters)
                destination = self.archive_root / timestamp / slot.root.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(slot.root, destination, dirs_exist_ok=True)
                for path in destination.rglob("*"):
                    if path.is_file():
                        path.chmod(0o444)
            except Exception:
                failures.append(slot.root.name)
        status = "COMPLETED" if not failures else ("PARTIAL" if imported_projects else "FAILED")
        return LegacyImportReport(status, imported_projects, imported_chapters, tuple(mismatches), tuple(failures))
