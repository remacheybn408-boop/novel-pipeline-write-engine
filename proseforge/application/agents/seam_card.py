"""跨章接缝卡（Phase 2）：上一章结尾锚点 → 注入写作/审校/改写的轻量卡。

现状空洞：只有 scene_writer 拿上一章全文（role_handlers
._load_previous_chapter_text），评审/改写/审校都拿不到；「本章开头 vs
上一章结尾」的接缝没有任何专门把守。本模块把上一章的结尾状态提炼成
一张小卡（结构化锚点 + 结尾原文摘录），三处注入：

- scene_writer（goal_hint 追加，与硬事实卡同款「始终在场」）
- continuity_reviewer（接缝审计：断裂以 seam_break findings 上报）
- chief_editor 改写（改写不得改断接缝）

数据来：上一章 active 版本（标题/摘要/结尾 800 字）+ story_bible 的
chapter_fact 台账（时间锚/未结伏笔/人物位置，summarize_chapter 沉淀）。
"""

from __future__ import annotations

import json
from typing import Any

SEAM_ENDING_MAX_CHARS = 800  # 上一章结尾原文摘录上限
SEAM_SUMMARY_MAX_CHARS = 200  # 上一章 L0 摘要注入上限
SEAM_LIST_MAX_ITEMS = 4  # 未结伏笔/人物位置各注入条数上限
SEAM_ANCHOR_VALUE_MAX_CHARS = 60  # 单条锚点值上限


async def _find_previous_chapter(session, *, project_id: str, current_no: int | None) -> tuple[int, str, str, str] | None:
    """当前章之前最近一章的 (chapter_no, title, summary, content)；无则 None。"""
    from sqlalchemy import select

    from proseforge.infrastructure.database.models.chapter import (
        ChapterModel,
        ChapterVersionModel,
    )

    rows = await session.scalars(
        select(ChapterModel)
        .where(ChapterModel.project_id == project_id, ChapterModel.active_version_id.isnot(None))
        .order_by(ChapterModel.chapter_no.desc())
    )
    chapter = next((row for row in rows if current_no is None or row.chapter_no < current_no), None)
    if chapter is None:
        return None
    version = await session.get(ChapterVersionModel, chapter.active_version_id)
    if version is None or not str(version.content or "").strip():
        return None
    return chapter.chapter_no, str(chapter.title or ""), str(version.summary or "").strip(), str(version.content).strip()


async def _load_chapter_fact(session, *, project_id: str, chapter_no: int) -> dict[str, Any]:
    """story_bible 台账的 chapter_fact（时间锚/未结伏笔/人物位置）；缺失返回 {}。"""
    from sqlalchemy import select

    from proseforge.infrastructure.database.models.story_bible import (
        StoryBibleEntryModel,
    )

    row = await session.scalar(
        select(StoryBibleEntryModel).where(
            StoryBibleEntryModel.project_id == project_id,
            StoryBibleEntryModel.kind == "chapter_fact",
            StoryBibleEntryModel.key == f"ch{chapter_no}",
        )
    )
    if row is None:
        return {}
    try:
        value = json.loads(row.value_json or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


async def load_seam_card(context: dict[str, object], goal_text: str) -> tuple[str, bool]:
    """渲染【跨章接缝卡】；返回 (卡文本, 摘要滞后标记)。

    返回 ("", False) 的情形：无 uow_factory（纯测试 context）、无 project_id、
    首章、前章无 active 版本。摘要滞后=True 表示前章 L0 摘要尚未落库
    （异步摘要链路延迟），调用方应记 context.summary_lagging 审计事件。
    """
    from proseforge.application.agents.review_target import parse_chapter_no

    uow_factory = context.get("uow_factory")
    run = context.get("run")
    if uow_factory is None or not isinstance(run, dict):
        return "", False
    project_id = str(run.get("project_id", "") or "")
    if not project_id:
        return "", False
    current_no = parse_chapter_no(goal_text)
    async with uow_factory() as uow:  # type: ignore[operator]
        previous = await _find_previous_chapter(uow.session, project_id=project_id, current_no=current_no)
        if previous is None:
            return "", False
        prev_no, title, summary, content = previous
        chapter_fact = await _load_chapter_fact(uow.session, project_id=project_id, chapter_no=prev_no)

    lines = [f"【跨章接缝卡】上一章（第{prev_no}章《{title}》）结尾锚点——本章开头必须自然承接，不得断裂或跳跃："]
    time_anchor = str(chapter_fact.get("time_anchor", "")).strip()
    if time_anchor:
        lines.append(f"- 时间锚：{time_anchor[:SEAM_ANCHOR_VALUE_MAX_CHARS]}")
    open_loops = chapter_fact.get("open_loops")
    if isinstance(open_loops, list) and open_loops:
        loops = [str(item).strip()[:SEAM_ANCHOR_VALUE_MAX_CHARS] for item in open_loops[:SEAM_LIST_MAX_ITEMS] if str(item).strip()]
        if loops:
            lines.append("- 未结伏笔：" + "；".join(loops))
    locations = chapter_fact.get("locations")
    if isinstance(locations, dict) and locations:
        spots = [f"{person}→{str(place)[:SEAM_ANCHOR_VALUE_MAX_CHARS]}" for person, place in list(locations.items())[:SEAM_LIST_MAX_ITEMS]]
        lines.append("- 人物位置：" + "；".join(spots))
    summary_lagging = not summary
    if summary:
        lines.append(f"- 上章摘要：{summary[:SEAM_SUMMARY_MAX_CHARS]}")
    ending = content[-SEAM_ENDING_MAX_CHARS:] if len(content) > SEAM_ENDING_MAX_CHARS else content
    lines.append("- 上章结尾原文：")
    lines.append(ending)
    return "\n".join(lines), summary_lagging
