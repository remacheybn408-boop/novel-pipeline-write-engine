"""load_builtin_skills: frontmatter parsing, name fallback, enable defaults, real pack."""

from __future__ import annotations

from pathlib import Path

from proseforge.application.plugins.builtin_skills import (
    DEFAULT_SKILLS_DIR,
    load_builtin_skills,
)
from proseforge.application.work.retriever import NARRATIVE_RAG_SKILL_KEY

EXPECTED_PACK_SKILL_COUNT = 87  # 22 题材包 + 60 文风技法卡 + 4 craft 工具包 + builtin-narrative-rag

SKILL_MD = """---
name: demo-skill
description: Demo skill for parsing tests.
---

# 演示技能

正文内容。
"""


def _write_skill(root: Path, dirname: str, text: str) -> None:
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")


def test_parse_frontmatter_and_h1(tmp_path: Path) -> None:
    _write_skill(tmp_path, "demo-skill", SKILL_MD)

    skills = load_builtin_skills(str(tmp_path))

    assert len(skills) == 1
    skill = skills[0]
    assert skill.skill_key == "demo-skill"
    assert skill.name == "演示技能"  # H1 wins over frontmatter name
    assert skill.description == "Demo skill for parsing tests."
    assert skill.content.startswith("# 演示技能")
    assert "---" not in skill.content


def test_sorted_by_skill_key_and_dir_without_skill_md_skipped(tmp_path: Path) -> None:
    _write_skill(tmp_path, "zeta-skill", SKILL_MD.replace("demo-skill", "zeta-skill"))
    _write_skill(tmp_path, "alpha-skill", SKILL_MD.replace("demo-skill", "alpha-skill"))
    (tmp_path / "empty-dir").mkdir()  # no SKILL.md -> skipped

    skills = load_builtin_skills(str(tmp_path))

    assert [skill.skill_key for skill in skills] == ["alpha-skill", "zeta-skill"]


def test_name_fallbacks_and_empty_body_skipped(tmp_path: Path) -> None:
    # No H1: fall back to frontmatter name.
    _write_skill(tmp_path, "no-h1", "---\nname: fallback-name\ndescription: d\n---\n\n只有正文没有标题。\n")
    # No frontmatter at all: fall back to directory name, empty description.
    _write_skill(tmp_path, "bare-dir-name", "没有 frontmatter 的正文。\n")
    # Frontmatter with empty body: skipped.
    _write_skill(tmp_path, "empty-body", "---\nname: empty\ndescription: d\n---\n\n   \n")

    skills = load_builtin_skills(str(tmp_path))

    by_key = {skill.skill_key: skill for skill in skills}
    assert set(by_key) == {"no-h1", "bare-dir-name"}
    assert by_key["no-h1"].name == "fallback-name"
    assert by_key["bare-dir-name"].name == "bare-dir-name"
    assert by_key["bare-dir-name"].description == ""


def test_missing_directory_returns_empty(tmp_path: Path) -> None:
    assert load_builtin_skills(str(tmp_path / "does-not-exist")) == []


def test_category_fiction_vs_tool(tmp_path: Path) -> None:
    # Genre packs (小说类) end in -fiction-writing; craft/system packs are 工具类.
    _write_skill(tmp_path, "wuxia-fiction-writing", SKILL_MD)
    _write_skill(tmp_path, "craft-foreshadowing", SKILL_MD)
    _write_skill(tmp_path, "builtin-narrative-rag", SKILL_MD)

    by_key = {skill.skill_key: skill for skill in load_builtin_skills(str(tmp_path))}

    assert by_key["wuxia-fiction-writing"].category == "fiction"
    assert by_key["craft-foreshadowing"].category == "tool"
    assert by_key["builtin-narrative-rag"].category == "tool"


def test_real_packs_categories_split() -> None:
    skills = load_builtin_skills(DEFAULT_SKILLS_DIR)

    fiction = [skill for skill in skills if skill.category == "fiction"]
    tool = [skill for skill in skills if skill.category == "tool"]

    assert len(fiction) + len(tool) == EXPECTED_PACK_SKILL_COUNT
    assert len(fiction) == 22  # all *-fiction-writing genre packs（含 memoir）
    tool_keys = {skill.skill_key for skill in tool}
    assert len(tool_keys) == 65  # 60 style-* 技法卡 + 4 craft-* + builtin-narrative-rag
    assert {
        "builtin-narrative-rag",
        "craft-dialogue-polishing",
        "craft-foreshadowing",
        "craft-pacing-control",
        "craft-payoff-design",
    } <= tool_keys
    assert len([key for key in tool_keys if key.startswith("style-")]) == 60


def test_real_packs_skills_dir_loads_all() -> None:
    skills = load_builtin_skills(DEFAULT_SKILLS_DIR)

    assert len(skills) == EXPECTED_PACK_SKILL_COUNT
    for skill in skills:
        assert skill.name, skill.skill_key
        assert skill.description, skill.skill_key
        assert skill.content, skill.skill_key


def test_enable_defaults_only_narrative_rag_on() -> None:
    # Mirrors api/routes/plugins.py: no state row -> only narrative-rag enabled.
    enabled_map: dict[str, bool] = {}
    skills = load_builtin_skills(DEFAULT_SKILLS_DIR)

    enabled = {skill.skill_key: enabled_map.get(skill.skill_key, skill.skill_key == NARRATIVE_RAG_SKILL_KEY) for skill in skills}

    assert NARRATIVE_RAG_SKILL_KEY in enabled
    assert enabled[NARRATIVE_RAG_SKILL_KEY] is True
    assert [key for key, is_on in enabled.items() if is_on] == [NARRATIVE_RAG_SKILL_KEY]
