from __future__ import annotations

from dataclasses import dataclass

from proseforge.domain.common.ids import new_id


@dataclass(frozen=True)
class Project:
    id: str
    owner_id: str
    slug: str
    title: str
    genre: str = ""
    style: str = ""
    language: str = "zh-CN"
    status: str = "ACTIVE"
    mode: str = "work"  # work | chat（0030 起；存量归 work）
    writing_model_provider: str | None = None
    writing_model_id: str | None = None
    model_locked_at: object | None = None
    model_lock_source: str | None = None  # outline_import | first_chapter

    @property
    def writing_model_locked(self) -> bool:
        return self.model_locked_at is not None

    @classmethod
    def create(cls, *, owner_id: str, slug: str, title: str, genre: str = "", style: str = "", mode: str = "work") -> Project:
        return cls(new_id(), owner_id, slug, title, genre, style, mode=mode)
