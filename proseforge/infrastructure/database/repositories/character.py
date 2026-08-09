"""Character persistence: CRUD + auto-extraction merge."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.characters.entity import Character
from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.character import CharacterModel
from proseforge.infrastructure.database.models.project import ProjectModel


class SqlAlchemyCharacterRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_owned(self, project_id: str, owner_id: str, *, include_archived: bool = False) -> list[Character]:
        query = (
            select(CharacterModel)
            .join(ProjectModel, CharacterModel.project_id == ProjectModel.id)
            .where(CharacterModel.project_id == project_id, ProjectModel.owner_id == owner_id)
            .order_by(CharacterModel.name, CharacterModel.id)
        )
        if not include_archived:
            query = query.where(CharacterModel.status == "active")
        rows = await self.session.scalars(query)
        return [self._entity(row) for row in rows]

    async def list_for_project(self, project_id: str) -> list[Character]:
        """Worker-side listing (job rows are the authority, no owner check).
        Active rows only: archived characters never re-enter retrieval,
        scene packs or conflict matching."""
        rows = await self.session.scalars(
            select(CharacterModel).where(
                CharacterModel.project_id == project_id,
                CharacterModel.status == "active",
            ).order_by(CharacterModel.name, CharacterModel.id)
        )
        return [self._entity(row) for row in rows]

    async def get_owned(self, character_id: str, project_id: str, owner_id: str) -> Character | None:
        row = await self._row(character_id)
        if row is None or row.project_id != project_id:
            return None
        owner = await self.session.scalar(select(ProjectModel.owner_id).where(ProjectModel.id == project_id))
        if owner != owner_id:
            return None
        return self._entity(row)

    async def get_by_name(self, project_id: str, name: str) -> Character | None:
        row = await self.session.scalar(
            select(CharacterModel).where(CharacterModel.project_id == project_id, CharacterModel.name == name)
        )
        return None if row is None else self._entity(row)

    async def add(self, character: Character) -> Character:
        now = datetime.now(UTC)
        self.session.add(CharacterModel(
            id=character.id, project_id=character.project_id, name=character.name,
            aliases_json=json.dumps(character.aliases, ensure_ascii=False),
            summary=character.summary, role=character.role,
            first_seen_chapter=character.first_seen_chapter, last_seen_chapter=character.last_seen_chapter,
            status=character.status, source=character.source, confidence=character.confidence,
            created_at=now, updated_at=now,
        ))
        await self.session.flush()
        return character

    async def update_owned(self, character_id: str, project_id: str, owner_id: str, *, name: str | None = None, aliases: list[str] | None = None, summary: str | None = None, role: str | None = None, status: str | None = None) -> Character | None:
        row = await self._row(character_id)
        if row is None or row.project_id != project_id:
            return None
        owner = await self.session.scalar(select(ProjectModel.owner_id).where(ProjectModel.id == project_id))
        if owner != owner_id:
            return None
        if name is not None:
            row.name = name
        if aliases is not None:
            row.aliases_json = json.dumps(aliases, ensure_ascii=False)
        if summary is not None:
            row.summary = summary
        if role is not None:
            row.role = role
        if status is not None:
            row.status = status
        # A human edit always promotes the row to user authority.
        row.source = "user"
        row.confidence = 1.0
        row.updated_at = datetime.now(UTC)
        await self.session.flush()
        return self._entity(row)

    async def delete_owned(self, character_id: str, project_id: str, owner_id: str) -> bool:
        row = await self._row(character_id)
        if row is None or row.project_id != project_id:
            return False
        owner = await self.session.scalar(select(ProjectModel.owner_id).where(ProjectModel.id == project_id))
        if owner != owner_id:
            return False
        await self.session.execute(delete(CharacterModel).where(CharacterModel.id == character_id))
        await self.session.flush()
        return True

    async def merge_extracted(self, project_id: str, *, name: str, aliases: list[str], summary: str, role: str, chapter_no: int) -> Character:
        """Auto-extraction upsert. Matches an existing row by name or alias:
        user rows keep every field but last_seen_chapter; auto rows absorb
        new aliases (union) and refresh summary/role. Unmatched names insert
        a source="auto", confidence=0.6 row.
        """
        rows = await self.session.scalars(select(CharacterModel).where(CharacterModel.project_id == project_id))
        needle = name.strip().lower()
        alias_needles = {alias.strip().lower() for alias in aliases}
        matched: CharacterModel | None = None
        for row in rows:
            known = {row.name.strip().lower(), *(a.strip().lower() for a in json.loads(row.aliases_json or "[]"))}
            if needle in known or known & alias_needles:
                matched = row
                break
        now = datetime.now(UTC)
        if matched is None:
            character = Character(
                id=new_id(), project_id=project_id, name=name, aliases=list(aliases),
                summary=summary, role=role,
                first_seen_chapter=chapter_no, last_seen_chapter=chapter_no,
                source="auto", confidence=0.6,
            )
            await self.add(character)
            return character
        if matched.source != "user":
            existing_aliases = json.loads(matched.aliases_json or "[]")
            known_lower = {a.strip().lower() for a in existing_aliases}
            merged = existing_aliases + [a for a in aliases if a.strip().lower() not in known_lower]
            matched.aliases_json = json.dumps(merged, ensure_ascii=False)
            if summary:
                matched.summary = summary
            if role:
                matched.role = role
        matched.last_seen_chapter = chapter_no
        matched.updated_at = now
        await self.session.flush()
        return self._entity(matched)

    async def touch_last_seen(self, character_id: str, chapter_no: int) -> None:
        row = await self._row(character_id)
        if row is not None:
            row.last_seen_chapter = chapter_no
            row.updated_at = datetime.now(UTC)
            await self.session.flush()

    async def _row(self, character_id: str) -> CharacterModel | None:
        return await self.session.scalar(select(CharacterModel).where(CharacterModel.id == character_id))

    @staticmethod
    def _entity(row: CharacterModel) -> Character:
        return Character(
            id=row.id,
            project_id=row.project_id,
            name=row.name,
            aliases=json.loads(row.aliases_json or "[]"),
            summary=row.summary,
            role=row.role,
            first_seen_chapter=row.first_seen_chapter,
            last_seen_chapter=row.last_seen_chapter,
            status=row.status,
            source=row.source,
            confidence=row.confidence,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
