from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.chapter.entity import Chapter, ChapterVersion
from proseforge.domain.chapter.paragraphs import anchors_json
from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.dialect import capabilities_for_engine
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.recap import RecapRollupModel
from proseforge.infrastructure.database.models.remaining import AuditLogModel
from proseforge.infrastructure.database.models.retrieval import (
    RetrievalChunkModel,
    RetrievalDocumentModel,
    RetrievalJobModel,
)


class SqlAlchemyChapterRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, chapter: Chapter) -> Chapter:
        self.session.add(ChapterModel(**chapter.__dict__))
        return chapter

    async def list_owned(self, project_id: str, owner_id: str) -> list[Chapter]:
        rows = await self.session.scalars(
            select(ChapterModel)
            .join(ProjectModel, ProjectModel.id == ChapterModel.project_id)
            .where(ChapterModel.project_id == project_id, ProjectModel.owner_id == owner_id)
            .order_by(ChapterModel.chapter_no)
        )
        return [self._chapter(row) for row in rows]

    async def get_owned(self, chapter_id: str, owner_id: str) -> Chapter | None:
        row = await self.session.scalar(
            select(ChapterModel)
            .join(ProjectModel, ProjectModel.id == ChapterModel.project_id)
            .where(ChapterModel.id == chapter_id, ProjectModel.owner_id == owner_id)
        )
        return None if row is None else self._chapter(row)

    async def append_version(self, *, chapter_id: str, content: str) -> ChapterVersion:
        # PG 用事务级 advisory lock 串行化并发追加；SQLite 由数据库级写锁
        # 串行化写入者（WAL + busy_timeout），无需等价语句。
        if capabilities_for_engine(self.session.bind).supports_advisory_locks:
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:chapter_id))"),
                {"chapter_id": chapter_id},
            )
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing = await self.session.execute(
            select(ChapterVersionModel).where(
                ChapterVersionModel.chapter_id == chapter_id,
                ChapterVersionModel.content_hash == content_hash,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            return self._to_domain(row)

        maximum = await self.session.scalar(
            select(func.max(ChapterVersionModel.version_no)).where(ChapterVersionModel.chapter_id == chapter_id)
        )
        row = ChapterVersionModel(
            id=new_id(),
            chapter_id=chapter_id,
            version_no=(maximum or 0) + 1,
            content=content,
            content_hash=content_hash,
            word_count=len(content),
            # 段落锚点随版本写回生成（第 11 项前置）：定点改写与 evidence
            # 引用失效对账的反向索引都建在这份锚点上。
            paragraph_anchors=anchors_json(content),
        )
        self.session.add(row)
        await self.session.flush()
        return self._to_domain(row)

    async def list_versions(self, chapter_id: str, owner_id: str) -> list[ChapterVersion]:
        owned = await self.get_owned(chapter_id, owner_id)
        if owned is None:
            return []
        rows = await self.session.scalars(
            select(ChapterVersionModel)
            .where(ChapterVersionModel.chapter_id == chapter_id)
            .order_by(ChapterVersionModel.version_no.desc())
        )
        return [self._to_domain(row) for row in rows]

    async def get_version_owned(self, chapter_id: str, version_id: str, owner_id: str) -> ChapterVersion | None:
        row = await self.session.scalar(
            select(ChapterVersionModel)
            .join(ChapterModel, ChapterModel.id == ChapterVersionModel.chapter_id)
            .join(ProjectModel, ProjectModel.id == ChapterModel.project_id)
            .where(
                ChapterVersionModel.id == version_id,
                ChapterVersionModel.chapter_id == chapter_id,
                ProjectModel.owner_id == owner_id,
            )
        )
        return None if row is None else self._to_domain(row)

    async def set_active_version(self, chapter_id: str, version_id: str) -> None:
        row = await self.session.get(ChapterModel, chapter_id)
        if row is None:
            raise ValueError("chapter does not exist")
        row.active_version_id = version_id
        row.status = "DRAFTED"
        await self._on_active_version_changed(row, version_id)
        await self.session.flush()

    async def _on_active_version_changed(self, chapter: ChapterModel, version_id: str) -> None:
        """Memory-pyramid invalidation (phase-2 item 8), same commit as the
        write-back — the single funnel every set_active_version caller
        (executor / tasks / workflows executor / approve_proposal /
        importer / chapters API x2) goes through, so no call site can
        revise a chapter without this firing:

        1. enqueue the chapter indexing job for the new active version
           (idempotent here: an open PENDING job for the same chapter is
           reused — it reads the current active version at run time, so
           collapsing rapid edits into it is safe; a RUNNING job may
           already hold the pre-revision version, so it does NOT dedupe
           and a fresh job is queued. Call sites that also enqueue —
           chapters API / agent_executor / approve_proposal — ride the
           same row via enqueue_job's get-or-create, never double-queue);
        2. re-enqueue the chapter summary job for the new active version
           (idempotent downstream: an already-summarized version skips);
        3. flip every volume/book/era recap covering this chapter to
           stale=true, supersede its RAG document chunks (a stale recap
           must never leak back as retrieval evidence), and write one
           recap.stale audit event per invalidated recap.
        """
        now = datetime.now(UTC)
        open_index_job = await self.session.scalar(
            select(RetrievalJobModel.id).where(
                RetrievalJobModel.job_type == "index_chapter",
                RetrievalJobModel.source_type == "chapter",
                RetrievalJobModel.source_id == chapter.id,
                RetrievalJobModel.status == "pending",
            )
        )
        if open_index_job is None:
            self.session.add(RetrievalJobModel(
                id=new_id(),
                project_id=chapter.project_id,
                job_type="index_chapter",
                source_type="chapter",
                source_id=chapter.id,
                status="pending",
                attempt=0,
                requested_at=now,
            ))
        self.session.add(RetrievalJobModel(
            id=new_id(),
            project_id=chapter.project_id,
            job_type="summarize_chapter",
            source_type="chapter_version",
            source_id=version_id,
            status="pending",
            attempt=0,
            requested_at=now,
        ))
        stale_rollups = list((await self.session.scalars(
            select(RecapRollupModel).where(
                RecapRollupModel.project_id == chapter.project_id,
                RecapRollupModel.span_start <= chapter.chapter_no,
                RecapRollupModel.span_end >= chapter.chapter_no,
                RecapRollupModel.stale.is_(False),
            )
        )).all())
        for rollup in stale_rollups:
            rollup.stale = True
            rollup.updated_at = now
            superseded = 0
            document = await self.session.scalar(
                select(RetrievalDocumentModel).where(
                    RetrievalDocumentModel.project_id == chapter.project_id,
                    RetrievalDocumentModel.source_type == "recap_rollup",
                    RetrievalDocumentModel.source_id == rollup.id,
                    RetrievalDocumentModel.deleted_at.is_(None),
                )
            )
            if document is not None:
                document.status = "inactive"
                document.updated_at = now
                result = await self.session.execute(
                    update(RetrievalChunkModel)
                    .where(
                        RetrievalChunkModel.document_id == document.id,
                        RetrievalChunkModel.status == "active",
                    )
                    .values(status="superseded", updated_at=now)
                )
                superseded = int(result.rowcount or 0)
            self.session.add(AuditLogModel(
                id=new_id(), user_id=rollup.user_id, action="recap.stale",
                target_type="recap_rollup", target_id=rollup.id,
                payload=json.dumps({
                    "project_id": chapter.project_id, "level": rollup.level,
                    "span_start": rollup.span_start, "span_end": rollup.span_end,
                    "revised_chapter": chapter.chapter_no, "version_id": version_id,
                    "superseded_chunks": superseded,
                }, ensure_ascii=False, separators=(",", ":")),
            ))

    async def active_contents(self, project_id: str, owner_id: str) -> list[tuple[Chapter, str]]:
        chapters = await self.session.scalars(
            select(ChapterModel)
            .join(ProjectModel, ProjectModel.id == ChapterModel.project_id)
            .where(ChapterModel.project_id == project_id, ProjectModel.owner_id == owner_id)
            .order_by(ChapterModel.chapter_no)
        )
        result: list[tuple[Chapter, str]] = []
        for row in chapters:
            if row.active_version_id is None:
                result.append((self._chapter(row), ""))
                continue
            version = await self.session.get(ChapterVersionModel, row.active_version_id)
            result.append((self._chapter(row), version.content if version else ""))
        return result

    @staticmethod
    def _chapter(row: ChapterModel) -> Chapter:
        return Chapter(
            id=row.id,
            project_id=row.project_id,
            chapter_no=row.chapter_no,
            title=row.title,
            status=row.status,
            active_version_id=row.active_version_id,
        )

    @staticmethod
    def _to_domain(row: ChapterVersionModel) -> ChapterVersion:
        return ChapterVersion(
            id=row.id,
            chapter_id=row.chapter_id,
            version_no=row.version_no,
            content=row.content,
            content_hash=row.content_hash,
            word_count=row.word_count,
            summary=row.summary,
            paragraph_anchors=row.paragraph_anchors or "[]",
        )

    async def set_version_summary(self, version_id: str, summary: str) -> None:
        row = await self.session.get(ChapterVersionModel, version_id)
        if row is not None:
            row.summary = summary
            await self.session.flush()

    async def get_version(self, version_id: str) -> ChapterVersion | None:
        """Worker-side fetch (job rows are the authority, no owner check)."""
        row = await self.session.get(ChapterVersionModel, version_id)
        return None if row is None else self._to_domain(row)
