"""Standalone review/revise target resolution (chapter full-text injection).

Swarm "review"/"revise" intent runs have no upstream artifacts (the review
battery's tasks all declare ``depends_on=[]``), so without this module the
reviewers/rewriter had nothing to work on and produced empty reports. The
target chapter is resolved from ``run.chapter_id`` first, then from the
chapter number in the run goal ("第三章" / "第3章"); when no target can be
determined the caller fails the task loudly with a Chinese-readable reason
instead of spinning an empty pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select

from proseforge.application.agents.role_handlers import TaskContext
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)


@dataclass(frozen=True)
class ChapterTarget:
    """The chapter a standalone review/revise run should work on."""

    chapter_id: str
    chapter_no: int
    title: str
    content: str


# "第3章" / "第 三 章" — digits or Chinese numerals between 第 and 章.
_CHAPTER_NO_PATTERN = re.compile(r"第\s*([0-9]+|[零一二两三四五六七八九十百]+)\s*章")
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _chinese_to_int(text: str) -> int | None:
    """Chinese numeral -> int (supports the 1..999 shapes used in chapter
    titles: 三 / 十二 / 二十三 / 一百零五). Returns None when unparsable."""
    text = text.strip().lstrip("零")
    if not text:
        return None
    if "百" in text:
        head, _, tail = text.partition("百")
        hundreds = _CN_DIGIT.get(head) if head else 1
        if hundreds is None or hundreds == 0:
            return None
        rest = _chinese_to_int(tail) if tail else 0
        return None if rest is None else hundreds * 100 + rest
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGIT.get(head) if head else 1
        if tens is None or tens == 0:
            return None
        units = _CN_DIGIT.get(tail) if tail else 0
        if units is None:
            return None
        return tens * 10 + units
    if len(text) == 1:
        return _CN_DIGIT.get(text)
    return None


def parse_chapter_no(text: str) -> int | None:
    """First "第 X 章" chapter number in the text (digits or Chinese numerals)."""
    match = _CHAPTER_NO_PATTERN.search(text or "")
    if match is None:
        return None
    raw = match.group(1)
    if raw.isdigit():
        return int(raw)
    return _chinese_to_int(raw)


async def resolve_chapter_target(context: TaskContext, *, object_label: str) -> tuple[ChapterTarget | None, str | None]:
    """Resolve the chapter a standalone review/revise run works on.

    Priority: ``run.chapter_id``, then the chapter number parsed from the run
    goal. Returns ``(target, None)`` on success, ``(None, reason)`` with a
    Chinese-readable reason when no reviewable/rewritable target exists
    (no chapter reference, chapter missing, or chapter has no content yet).
    """
    run = context["run"]
    assert isinstance(run, dict)
    project_id = str(run.get("project_id") or "")
    goal = str(run.get("goal") or "")
    uow_factory = context["uow_factory"]
    assert callable(uow_factory)
    async with uow_factory() as uow:
        chapter_id = str(run.get("chapter_id") or "")
        chapter: ChapterModel | None
        if chapter_id:
            chapter = await uow.session.get(ChapterModel, chapter_id)
            if chapter is None or chapter.project_id != project_id:
                return None, f"运行关联的章节不存在，无法确定{object_label}对象"
        else:
            chapter_no = parse_chapter_no(goal)
            if chapter_no is None:
                return None, f"无法确定{object_label}对象：请求中没有指明章节（例如「{object_label}第三章」），运行也没有关联章节"
            chapter = await uow.session.scalar(
                select(ChapterModel).where(ChapterModel.project_id == project_id, ChapterModel.chapter_no == chapter_no)
            )
            if chapter is None:
                return None, f"第{chapter_no}章不存在，无法{object_label}：请先创建该章节"
        version = await uow.session.get(ChapterVersionModel, chapter.active_version_id) if chapter.active_version_id else None
        content = version.content if version is not None else ""
        if not content.strip():
            return None, f"第{chapter.chapter_no}章（{chapter.title}）还没有正文内容，无法{object_label}"
        return ChapterTarget(chapter_id=chapter.id, chapter_no=chapter.chapter_no, title=chapter.title, content=content), None
