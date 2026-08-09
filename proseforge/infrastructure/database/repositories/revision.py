from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.revision import (
    ReviewReportModel,
    RevisionProposalModel,
)


class SqlAlchemyRevisionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def _owned_query(self, proposal_id: str, owner_id: str):
        return select(RevisionProposalModel).join(ChapterModel, ChapterModel.id == RevisionProposalModel.chapter_id).join(ProjectModel, ProjectModel.id == ChapterModel.project_id).where(RevisionProposalModel.id == proposal_id, ProjectModel.owner_id == owner_id)

    async def get_owned(self, proposal_id: str, owner_id: str) -> RevisionProposalModel | None:
        return await self.session.scalar(self._owned_query(proposal_id, owner_id))

    async def get_owned_for_update(self, proposal_id: str, owner_id: str) -> RevisionProposalModel | None:
        # PostgreSQL emits FOR UPDATE; SQLite relies on its enclosing write transaction.
        return await self.session.scalar(self._owned_query(proposal_id, owner_id).with_for_update())

    async def list_owned(self, chapter_id: str, owner_id: str) -> list[RevisionProposalModel]:
        rows = await self.session.scalars(
            select(RevisionProposalModel).join(ChapterModel, ChapterModel.id == RevisionProposalModel.chapter_id).join(ProjectModel, ProjectModel.id == ChapterModel.project_id).where(RevisionProposalModel.chapter_id == chapter_id, ProjectModel.owner_id == owner_id).order_by(RevisionProposalModel.created_at)
        )
        return list(rows)

    async def create(self, *, chapter_id: str, base_version_id: str, before: str, after: str, rationale: str, hunks: list[dict[str, object]] | None = None, affected_facts: list[object] | None = None, context_snapshot_id: str | None = None) -> RevisionProposalModel:
        now = datetime.now(UTC)
        proposal = RevisionProposalModel(
            id=new_id(), chapter_id=chapter_id, base_version_id=base_version_id,
            before_hash=hashlib.sha256(before.encode("utf-8")).hexdigest(), after_text=after,
            after_hash=hashlib.sha256(after.encode("utf-8")).hexdigest(), rationale=rationale,
            status="PROPOSED", hunks_json=json.dumps(hunks if hunks is not None else [{"start": 0, "end": len(before), "replacement": after}], ensure_ascii=False),
            affected_facts_json=json.dumps(affected_facts or [], ensure_ascii=False), context_snapshot_id=context_snapshot_id,
            created_at=now, updated_at=now,
        )
        self.session.add(proposal)
        await self.session.flush()
        return proposal

    async def current_version(self, chapter_id: str, owner_id: str) -> ChapterVersionModel | None:
        return await self.session.scalar(
            select(ChapterVersionModel).join(ChapterModel, ChapterModel.id == ChapterVersionModel.chapter_id).join(ProjectModel, ProjectModel.id == ChapterModel.project_id).where(ChapterVersionModel.chapter_id == chapter_id, ChapterVersionModel.id == ChapterModel.active_version_id, ProjectModel.owner_id == owner_id)
        )

    async def create_review(self, *, project_id: str, scope: str, subject_type: str, subject_id: str, findings: list[dict[str, object]], scores: dict[str, object], model_snapshot: dict[str, object], context_snapshot_id: str | None = None, usage_call_id: str | None = None) -> ReviewReportModel:
        report = ReviewReportModel(id=new_id(), project_id=project_id, scope=scope, subject_type=subject_type, subject_id=subject_id, findings_json=json.dumps(findings, ensure_ascii=False), scores_json=json.dumps(scores, ensure_ascii=False), model_snapshot_json=json.dumps(model_snapshot, ensure_ascii=False), context_snapshot_id=context_snapshot_id, usage_call_id=usage_call_id, status="COMPLETED", created_at=datetime.now(UTC))
        self.session.add(report)
        await self.session.flush()
        return report

    async def get_review_owned(self, review_id: str, owner_id: str) -> ReviewReportModel | None:
        return await self.session.scalar(select(ReviewReportModel).join(ProjectModel, ProjectModel.id == ReviewReportModel.project_id).where(ReviewReportModel.id == review_id, ProjectModel.owner_id == owner_id))
