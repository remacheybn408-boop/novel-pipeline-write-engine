from __future__ import annotations

from dataclasses import dataclass

from proseforge.domain.common.ids import new_id


@dataclass(frozen=True)
class Chapter:
    id: str
    project_id: str
    chapter_no: int
    title: str
    status: str = "PLANNED"
    active_version_id: str | None = None

    @classmethod
    def create(cls, *, project_id: str, chapter_no: int, title: str) -> Chapter:
        return cls(new_id(), project_id, chapter_no, title)


@dataclass(frozen=True)
class ChapterVersion:
    id: str
    chapter_id: str
    version_no: int
    content: str
    content_hash: str
    word_count: int
    summary: str = ""
    # 段落锚点 JSON 串（domain.chapter.paragraphs.anchors_json 格式）；缺省空数组。
    paragraph_anchors: str = "[]"
