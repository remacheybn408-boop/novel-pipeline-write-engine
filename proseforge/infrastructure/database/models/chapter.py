from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from proseforge.infrastructure.database.base import Base


class ChapterModel(Base):
    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("project_id", "chapter_no", name="uq_chapters_project_id_chapter_no"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    chapter_no: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PLANNED", nullable=False)
    active_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 卷序号（迁移 0052）：写回时按 goal 卷标签解析落库；NULL = 未解析
    # （旧数据或无卷标签大纲），查询侧回退 goal 正则 / 固定 10 章一卷。
    volume_no: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ChapterVersionModel(Base):
    __tablename__ = "chapter_versions"
    __table_args__ = (
        UniqueConstraint("chapter_id", "version_no", name="uq_chapter_versions_chapter_id_version_no"),
        UniqueConstraint("chapter_id", "content_hash", name="uq_chapter_versions_chapter_id_content_hash"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chapter_id: Mapped[str] = mapped_column(String(64), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # 段落锚点索引（第 11 项前置，迁移 0049）：JSON 数组
    # [{paragraph_id, index, content_hash, start, end, chars}]，写回时随版本生成。
    paragraph_anchors: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
