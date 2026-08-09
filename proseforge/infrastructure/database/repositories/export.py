from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.export import ExportManifestModel


class SqlAlchemyExportManifestRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        *,
        project_id: str,
        user_id: str,
        format_name: str,
        template: str,
        title: str | None,
        author: str | None,
        locale: str,
        version_ids: list[str],
        content_hashes: dict[str, str],
        file_sha256: str,
        byte_size: int,
    ) -> ExportManifestModel:
        manifest = ExportManifestModel(
            id=new_id(),
            project_id=project_id,
            user_id=user_id,
            format=format_name,
            template=template,
            title=title,
            author=author,
            locale=locale,
            version_ids_json=json.dumps(version_ids, separators=(",", ":")),
            content_hashes_json=json.dumps(content_hashes, separators=(",", ":"), sort_keys=True),
            file_sha256=file_sha256,
            byte_size=byte_size,
            created_at=datetime.now(UTC),
        )
        self.session.add(manifest)
        await self.session.flush()
        return manifest

    async def get_owned(self, manifest_id: str, project_id: str, user_id: str) -> ExportManifestModel | None:
        return await self.session.scalar(
            select(ExportManifestModel).where(
                ExportManifestModel.id == manifest_id,
                ExportManifestModel.project_id == project_id,
                ExportManifestModel.user_id == user_id,
            )
        )
