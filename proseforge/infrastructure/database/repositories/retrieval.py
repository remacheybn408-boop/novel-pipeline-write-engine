"""Narrative RAG retrieval persistence (phase 1: indexing writes)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.retrieval import (
    RetrievalChunkModel,
    RetrievalDocumentModel,
    RetrievalJobModel,
)


class SqlAlchemyRetrievalRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    # -- jobs ---------------------------------------------------------------

    async def enqueue_job(self, *, project_id: str, job_type: str, source_type: str, source_id: str) -> RetrievalJobModel:
        """Get-or-create a pending job: a PENDING row with the same
        (job_type, source_type, source_id) is returned as-is instead of
        stacking a duplicate — chapter index jobs read the current active
        version at run time, so one pending row covers rapid successive
        edits. A running row does NOT dedupe: it may already hold the
        pre-revision version, so the caller gets a fresh pending row.

        The set_active_version funnel enqueues index_chapter centrally, so
        call sites that also enqueue for the post-commit dispatch id
        (chapters API / agent_executor / approve_proposal) ride the same
        row rather than double-queueing."""
        existing = await self.session.scalar(
            select(RetrievalJobModel).where(
                RetrievalJobModel.job_type == job_type,
                RetrievalJobModel.source_type == source_type,
                RetrievalJobModel.source_id == source_id,
                RetrievalJobModel.status == "pending",
            )
        )
        if existing is not None:
            return existing
        job = RetrievalJobModel(
            id=new_id(),
            project_id=project_id,
            job_type=job_type,
            source_type=source_type,
            source_id=source_id,
            status="pending",
            attempt=0,
            requested_at=datetime.now(UTC),
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def get_job(self, job_id: str) -> RetrievalJobModel | None:
        return await self.session.scalar(select(RetrievalJobModel).where(RetrievalJobModel.id == job_id))

    async def claim_job(self, job_id: str) -> bool:
        """Atomically claim a pending job (pending -> running, attempt + 1).

        Single conditional UPDATE: a concurrent claimant loses the race and
        gets False, so the same job can never run twice in parallel.
        """
        result = await self.session.execute(
            update(RetrievalJobModel)
            .where(RetrievalJobModel.id == job_id, RetrievalJobModel.status == "pending")
            .values(
                status="running",
                attempt=RetrievalJobModel.attempt + 1,
                started_at=datetime.now(UTC),
                error=None,
            )
        )
        return int(result.rowcount or 0) == 1

    async def bump_job_requested_at(self, job_id: str) -> None:
        """Re-stamp a pending job's requested_at (sweeper re-dispatch marker,
        so the row is not re-enqueued again before the next threshold)."""
        await self.session.execute(
            update(RetrievalJobModel)
            .where(RetrievalJobModel.id == job_id, RetrievalJobModel.status == "pending")
            .values(requested_at=datetime.now(UTC))
        )

    async def list_stale_pending_jobs(self, cutoff: datetime, *, limit: int) -> list[tuple[str, str, str]]:
        """(job_id, owner_id, job_type) for jobs still pending since before
        cutoff, oldest first. job_type rides along so the sweeper can route
        each row to its own queue task instead of one hardcoded handler."""
        from proseforge.infrastructure.database.models.project import ProjectModel

        rows = await self.session.execute(
            select(RetrievalJobModel.id, ProjectModel.owner_id, RetrievalJobModel.job_type)
            .join(ProjectModel, RetrievalJobModel.project_id == ProjectModel.id)
            .where(RetrievalJobModel.status == "pending", RetrievalJobModel.requested_at <= cutoff)
            .order_by(RetrievalJobModel.requested_at)
            .limit(limit)
        )
        return [(row.id, row.owner_id, row.job_type) for row in rows]

    async def rearm_stale_running_jobs(self, cutoff: datetime, *, limit: int) -> list[tuple[str, str, str]]:
        """(job_id, owner_id, job_type) for running jobs started before cutoff,
        atomically reset to pending so the sweeper can redispatch them.

        claim_job only transitions pending -> running, so a worker killed
        mid-job strands the row in running forever and its chapter is never
        indexed. Legit in-flight jobs are newer than the cutoff and stay
        untouched; a racing duplicate claimant loses the atomic UPDATE.
        """
        from proseforge.infrastructure.database.models.project import ProjectModel

        stale_ids = (
            select(RetrievalJobModel.id)
            .where(RetrievalJobModel.status == "running", RetrievalJobModel.started_at <= cutoff)
            .order_by(RetrievalJobModel.started_at)
            .limit(limit)
        )
        result = await self.session.execute(
            update(RetrievalJobModel)
            .where(RetrievalJobModel.id.in_(stale_ids), RetrievalJobModel.status == "running")
            .values(status="pending", requested_at=datetime.now(UTC))
            .returning(RetrievalJobModel.id)
        )
        job_ids = [row.id for row in result]
        if not job_ids:
            return []
        rows = await self.session.execute(
            select(RetrievalJobModel.id, ProjectModel.owner_id, RetrievalJobModel.job_type)
            .join(ProjectModel, RetrievalJobModel.project_id == ProjectModel.id)
            .where(RetrievalJobModel.id.in_(job_ids))
        )
        return [(row.id, row.owner_id, row.job_type) for row in rows]

    # -- chapter source (worker-side: the job row is the authority, no owner check)

    async def get_chapter_with_active_version(self, chapter_id: str) -> tuple[ChapterModel, ChapterVersionModel] | None:
        chapter = await self.session.scalar(select(ChapterModel).where(ChapterModel.id == chapter_id))
        if chapter is None or chapter.active_version_id is None:
            return None
        version = await self.session.scalar(
            select(ChapterVersionModel).where(ChapterVersionModel.id == chapter.active_version_id)
        )
        if version is None:
            return None
        return chapter, version

    # -- documents / chunks ---------------------------------------------------

    async def get_document(self, *, project_id: str, source_type: str, source_id: str) -> RetrievalDocumentModel | None:
        return await self.session.scalar(
            select(RetrievalDocumentModel).where(
                RetrievalDocumentModel.project_id == project_id,
                RetrievalDocumentModel.source_type == source_type,
                RetrievalDocumentModel.source_id == source_id,
                RetrievalDocumentModel.deleted_at.is_(None),
            )
        )

    async def upsert_document(
        self, *, project_id: str, source_type: str, source_id: str, source_version: str, title: str,
        authority_level: str = "canon", chapter_from: int | None = None, chapter_to: int | None = None,
    ) -> RetrievalDocumentModel:
        now = datetime.now(UTC)
        document = await self.get_document(project_id=project_id, source_type=source_type, source_id=source_id)
        if document is None:
            document = RetrievalDocumentModel(
                id=new_id(),
                project_id=project_id,
                source_type=source_type,
                source_id=source_id,
                source_version=source_version,
                title=title,
                status="active",
                authority_level=authority_level,
                chapter_from=chapter_from,
                chapter_to=chapter_to,
                created_at=now,
                updated_at=now,
            )
            self.session.add(document)
        else:
            document.source_version = source_version
            document.title = title
            document.status = "active"
            document.updated_at = now
        await self.session.flush()
        return document

    async def supersede_active_chunks(self, document_id: str) -> int:
        result = await self.session.execute(
            update(RetrievalChunkModel)
            .where(RetrievalChunkModel.document_id == document_id, RetrievalChunkModel.status == "active")
            .values(status="superseded", updated_at=datetime.now(UTC))
        )
        return int(result.rowcount or 0)

    # -- model-switch guard ---------------------------------------------------

    async def count_active_chunks_with_other_model(self, *, owner_id: str, embedding_model: str) -> int:
        from sqlalchemy import func

        from proseforge.infrastructure.database.models.project import ProjectModel

        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(RetrievalChunkModel)
                .join(ProjectModel, RetrievalChunkModel.project_id == ProjectModel.id)
                .where(
                    ProjectModel.owner_id == owner_id,
                    RetrievalChunkModel.status == "active",
                    RetrievalChunkModel.embedding_model != embedding_model,
                )
            ) or 0
        )

    async def get_indexed_model_for_owner(self, *, owner_id: str) -> str | None:
        """The embedding identity of the owner's active chunks (most common one), else None."""
        from sqlalchemy import func

        from proseforge.infrastructure.database.models.project import ProjectModel

        return await self.session.scalar(
            select(RetrievalChunkModel.embedding_model)
            .join(ProjectModel, RetrievalChunkModel.project_id == ProjectModel.id)
            .where(ProjectModel.owner_id == owner_id, RetrievalChunkModel.status == "active")
            .group_by(RetrievalChunkModel.embedding_model)
            .order_by(func.count().desc())
            .limit(1)
        )

    async def index_health_for_owner(self, *, owner_id: str) -> dict[str, int | bool]:
        """Index reconciliation counts for the owner: chapters that SHOULD be
        indexed (work-mode, active version) vs documents/chunks actually
        indexed. ``drift`` is True when they disagree — the read side returns
        empty evidence silently in that state, so surface it in settings."""
        from sqlalchemy import func

        from proseforge.infrastructure.database.models.project import ProjectModel

        owned = select(ProjectModel.id).where(ProjectModel.owner_id == owner_id).scalar_subquery()
        indexable = int(
            await self.session.scalar(
                select(func.count())
                .select_from(ChapterModel)
                .join(ProjectModel, ChapterModel.project_id == ProjectModel.id)
                .where(
                    ProjectModel.owner_id == owner_id,
                    ProjectModel.mode == "work",
                    ChapterModel.active_version_id.is_not(None),
                )
            ) or 0
        )
        documents = int(
            await self.session.scalar(
                select(func.count())
                .select_from(RetrievalDocumentModel)
                .where(RetrievalDocumentModel.project_id.in_(owned))
            ) or 0
        )
        chunks = int(
            await self.session.scalar(
                select(func.count())
                .select_from(RetrievalChunkModel)
                .where(RetrievalChunkModel.project_id.in_(owned), RetrievalChunkModel.status == "active")
            ) or 0
        )
        return {
            "indexable_chapters": indexable,
            "indexed_documents": documents,
            "active_chunks": chunks,
            "drift": indexable > 0 and (documents < indexable or chunks == 0),
        }

    async def count_indexable_chapters_for_project(self, *, project_id: str) -> int:
        """Chapters with an active version — the number the index should cover."""
        from sqlalchemy import func

        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(ChapterModel)
                .where(ChapterModel.project_id == project_id, ChapterModel.active_version_id.is_not(None))
            ) or 0
        )

    async def count_active_chunks_for_project(self, *, project_id: str) -> int:
        from sqlalchemy import func

        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(RetrievalChunkModel)
                .where(RetrievalChunkModel.project_id == project_id, RetrievalChunkModel.status == "active")
            ) or 0
        )

    # -- engine switch (clear + reindex) --------------------------------------

    async def delete_documents_for_owner(self, *, owner_id: str) -> int:
        """Hard-delete every retrieval chunk/document under the owner's projects."""
        from sqlalchemy import delete

        from proseforge.infrastructure.database.models.project import ProjectModel

        project_ids = select(ProjectModel.id).where(ProjectModel.owner_id == owner_id).scalar_subquery()
        await self.session.execute(delete(RetrievalChunkModel).where(RetrievalChunkModel.project_id.in_(project_ids)))
        result = await self.session.execute(delete(RetrievalDocumentModel).where(RetrievalDocumentModel.project_id.in_(project_ids)))
        return int(result.rowcount or 0)

    async def delete_unfinished_jobs_for_owner(self, *, owner_id: str) -> int:
        """Hard-delete pending/running retrieval_jobs under the owner's
        projects (engine-switch rebuild: in-flight jobs of the old engine
        must not rewrite documents with a stale identity)."""
        from sqlalchemy import delete

        from proseforge.infrastructure.database.models.project import ProjectModel

        project_ids = select(ProjectModel.id).where(ProjectModel.owner_id == owner_id).scalar_subquery()
        result = await self.session.execute(
            delete(RetrievalJobModel).where(
                RetrievalJobModel.project_id.in_(project_ids),
                RetrievalJobModel.status.in_(["pending", "running"]),
            )
        )
        return int(result.rowcount or 0)

    async def count_unfinished_jobs_for_owner(self, *, owner_id: str) -> int:
        """Count pending/running retrieval_jobs under the owner's projects
        (rebuild-in-flight signal for the drift-alarm suppression)."""
        from sqlalchemy import func

        from proseforge.infrastructure.database.models.project import ProjectModel

        project_ids = select(ProjectModel.id).where(ProjectModel.owner_id == owner_id).scalar_subquery()
        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(RetrievalJobModel)
                .where(
                    RetrievalJobModel.project_id.in_(project_ids),
                    RetrievalJobModel.status.in_(["pending", "running"]),
                )
            ) or 0
        )

    async def requeue_failed_jobs_with_error(self, *, owner_id: str, error: str) -> int:
        """failed -> pending for the owner's jobs whose terminal error matches
        exactly (used when the cause — e.g. a missing embedding credential —
        has been fixed; the sweeper re-dispatches them naturally)."""
        from proseforge.infrastructure.database.models.project import ProjectModel

        project_ids = select(ProjectModel.id).where(ProjectModel.owner_id == owner_id).scalar_subquery()
        result = await self.session.execute(
            update(RetrievalJobModel)
            .where(
                RetrievalJobModel.project_id.in_(project_ids),
                RetrievalJobModel.status == "failed",
                RetrievalJobModel.error == error,
            )
            .values(status="pending", error=None)
        )
        return int(result.rowcount or 0)

    async def requeue_failed_jobs_for_owner(self, *, owner_id: str, job_type: str = "index_chapter") -> int:
        """failed -> pending for ALL of the owner's failed jobs of a type,
        regardless of the error string. A settings save may have fixed any
        class of cause (credential, model download, server port), and the
        terminal-error exact match requeue leaves every other failure class
        with no revival path. Re-armed jobs fail closed again if the cause
        persists, so this is safe to run on every save."""
        from proseforge.infrastructure.database.models.project import ProjectModel

        project_ids = select(ProjectModel.id).where(ProjectModel.owner_id == owner_id).scalar_subquery()
        result = await self.session.execute(
            update(RetrievalJobModel)
            .where(
                RetrievalJobModel.project_id.in_(project_ids),
                RetrievalJobModel.status == "failed",
                RetrievalJobModel.job_type == job_type,
            )
            .values(status="pending", error=None)
        )
        return int(result.rowcount or 0)

    async def list_indexable_chapters_for_owner(self, *, owner_id: str) -> list[tuple[str, str]]:
        """(project_id, chapter_id) for chapters with an active version in work-mode projects."""
        from proseforge.infrastructure.database.models.project import ProjectModel

        rows = await self.session.execute(
            select(ChapterModel.project_id, ChapterModel.id)
            .join(ProjectModel, ChapterModel.project_id == ProjectModel.id)
            .where(
                ProjectModel.owner_id == owner_id,
                ProjectModel.mode == "work",
                ChapterModel.active_version_id.is_not(None),
            )
        )
        return [(row.project_id, row.id) for row in rows]

    # -- canon conflicts ------------------------------------------------------

    async def add_conflict_if_open_absent(
        self, *, project_id: str, candidate_source: str, conflicting_source: str,
        field_or_claim: str, evidence: dict,
    ) -> bool:
        """Insert an open canon_conflicts row unless an identical open row
        (same candidate+conflicting+field) already exists."""
        from proseforge.infrastructure.database.models.retrieval import (
            CanonConflictModel,
        )

        existing = await self.session.scalar(
            select(CanonConflictModel).where(
                CanonConflictModel.project_id == project_id,
                CanonConflictModel.candidate_source == candidate_source,
                CanonConflictModel.conflicting_source == conflicting_source,
                CanonConflictModel.field_or_claim == field_or_claim,
                CanonConflictModel.status == "open",
            )
        )
        if existing is not None:
            return False
        self.session.add(CanonConflictModel(
            id=new_id(),
            project_id=project_id,
            candidate_source=candidate_source,
            conflicting_source=conflicting_source,
            field_or_claim=field_or_claim,
            evidence_json=json.dumps(evidence, ensure_ascii=False),
            status="open",
        ))
        await self.session.flush()
        return True

    async def list_conflicts(self, *, project_id: str, owner_id: str, status: str | None = "open"):
        from proseforge.infrastructure.database.models.project import ProjectModel
        from proseforge.infrastructure.database.models.retrieval import (
            CanonConflictModel,
        )

        query = (
            select(CanonConflictModel)
            .join(ProjectModel, CanonConflictModel.project_id == ProjectModel.id)
            .where(CanonConflictModel.project_id == project_id, ProjectModel.owner_id == owner_id)
            .order_by(CanonConflictModel.id)
        )
        if status is not None:
            query = query.where(CanonConflictModel.status == status)
        rows = await self.session.scalars(query)
        return list(rows)

    async def get_conflict_owned(self, conflict_id: str, *, project_id: str, owner_id: str):
        from proseforge.infrastructure.database.models.project import ProjectModel
        from proseforge.infrastructure.database.models.retrieval import (
            CanonConflictModel,
        )

        return await self.session.scalar(
            select(CanonConflictModel)
            .join(ProjectModel, CanonConflictModel.project_id == ProjectModel.id)
            .where(
                CanonConflictModel.id == conflict_id,
                CanonConflictModel.project_id == project_id,
                ProjectModel.owner_id == owner_id,
            )
        )

    async def add_chunk(
        self, *, project_id: str, document_id: str, chunk_index: int, content: str,
        embedding: list[float] | None, embedding_model: str, embedding_version: str,
        token_count: int, content_hash: str, metadata_json: str = "{}",
    ) -> RetrievalChunkModel:
        now = datetime.now(UTC)
        chunk = RetrievalChunkModel(
            id=new_id(),
            project_id=project_id,
            document_id=document_id,
            chunk_index=chunk_index,
            content=content,
            summary="",
            metadata_json=metadata_json,
            search_text=content,
            embedding=embedding,
            embedding_model=embedding_model,
            embedding_version=embedding_version,
            token_count=token_count,
            content_hash=content_hash,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.session.add(chunk)
        await self.session.flush()
        return chunk
