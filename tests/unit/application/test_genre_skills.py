"""genre_skills：中文题材→题材包映射、SKILL.md excerpt 截断、goal 题材行解析。"""

from __future__ import annotations

import os
from pathlib import Path

from proseforge.application.agents.genre_skills import (
    DEFAULT_SKILLS_DIR,
    DEFAULT_STYLE_SLUGS,
    GENRE_SKILL_MAP,
    GENRE_STYLE_MAP,
    STYLE_EXCERPT_MAX_CHARS,
    genre_from_goal,
    genre_skill_excerpt,
    genre_style_excerpt,
    skill_key_for_genre,
)


def test_genre_from_goal_parses_genre_line():
    goal = "写第3章《风起》\n本章大纲：雨夜接头\n题材：言情\n全书大纲：……"
    assert genre_from_goal(goal) == "言情"
    assert genre_from_goal("写第3章《风起》") == ""  # 无题材行
    assert genre_from_goal("") == ""


def test_skill_key_for_genre_keyword_mapping():
    assert skill_key_for_genre("仙侠") == "xianxia-fiction-writing"
    assert skill_key_for_genre("修真") == "xianxia-fiction-writing"
    assert skill_key_for_genre("都市甜宠言情") == "romance-fiction-writing"  # 组合文本先中情感类
    assert skill_key_for_genre("古言宫斗") == "ancient-romance-fiction-writing"
    assert skill_key_for_genre("") == ""
    assert skill_key_for_genre("菜谱") == ""  # 无映射：安静跳过


def test_bundled_skill_map_targets_exist():
    # 映射表里的每个目录都必须在 packs/skills 下真实存在且有 SKILL.md
    missing = [key for _, key in GENRE_SKILL_MAP if not (Path(DEFAULT_SKILLS_DIR) / key / "SKILL.md").is_file()]
    assert missing == []


def _write_pack(skills_dir: Path, pack_key: str, body: str) -> None:
    pack = skills_dir / pack_key
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "SKILL.md").write_text(f"---\nname: {pack_key}\ndescription: d\n---\n{body}", encoding="utf-8")


def test_excerpt_reads_body_without_frontmatter(tmp_path):
    _write_pack(tmp_path, "romance-fiction-writing", "# 爱情小说\n正文要点。")
    assert genre_skill_excerpt("言情", skills_dir=str(tmp_path)) == "# 爱情小说\n正文要点。"


def test_excerpt_truncates_to_max_chars(tmp_path):
    _write_pack(tmp_path, "xianxia-fiction-writing", "仙" * 2000)
    excerpt = genre_skill_excerpt("仙侠", max_chars=1200, skills_dir=str(tmp_path))
    assert len(excerpt) == 1200
    assert excerpt.endswith("…")
    assert "name:" not in excerpt  # frontmatter 不进入注入文本


def test_excerpt_unmapped_or_missing_pack_returns_empty(tmp_path):
    assert genre_skill_excerpt("", skills_dir=str(tmp_path)) == ""
    assert genre_skill_excerpt("菜谱", skills_dir=str(tmp_path)) == ""  # 映射不上
    assert genre_skill_excerpt("言情", skills_dir=str(tmp_path)) == ""  # 映射命中但包缺失


def test_excerpt_cache_invalidates_on_mtime_change(tmp_path):
    _write_pack(tmp_path, "romance-fiction-writing", "v1 要点")
    assert genre_skill_excerpt("言情", skills_dir=str(tmp_path)) == "v1 要点"
    path = tmp_path / "romance-fiction-writing" / "SKILL.md"
    _write_pack(tmp_path, "romance-fiction-writing", "v2 要点")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 10))
    assert genre_skill_excerpt("言情", skills_dir=str(tmp_path)) == "v2 要点"


def test_excerpt_bundled_romance_pack_loads():
    # 真实仓库包：言情 → romance-fiction-writing/SKILL.md 能产出非空摘录
    excerpt = genre_skill_excerpt("言情")
    assert excerpt
    assert "爱情小说" in excerpt


def test_review_user_prompt_renders_genre_block():
    # review_handlers 的 genre_block 注入点：非空即进入评审 user prompt
    from proseforge.application.agents.review_handlers import _review_user_prompt

    run = {"id": "run-1", "goal_hash": "g" * 64}
    prompt = _review_user_prompt("continuity_reviewer", "review_continuity", run, [], genre_block="【题材写作指引】X")
    assert "【题材写作指引】X" in prompt
    prompt_without = _review_user_prompt("continuity_reviewer", "review_continuity", run, [])
    assert "【题材写作指引】" not in prompt_without


# ---------------------------------------------------------------------------
# 文风技法卡：GENRE_STYLE_MAP 映射、合并摘要体积、缺省回退、批量 frontmatter 校验
# ---------------------------------------------------------------------------


def test_style_map_covers_every_registered_genre_pack():
    # GENRE_SKILL_MAP 注册的每个题材包都必须有技法卡覆盖
    uncovered = [key for _, key in GENRE_SKILL_MAP if key not in GENRE_STYLE_MAP]
    assert uncovered == []


def test_style_map_slugs_exist_on_disk():
    # 映射表 + 缺省回退里的每张卡都必须在 packs/skills 下真实存在
    slugs = {slug for slugs in GENRE_STYLE_MAP.values() for slug in slugs} | set(DEFAULT_STYLE_SLUGS)
    missing = [slug for slug in sorted(slugs) if not (Path(DEFAULT_SKILLS_DIR) / slug / "SKILL.md").is_file()]
    assert missing == []


def test_style_excerpt_mapped_genre_hits_configured_cards():
    # 武侠 → 汪曾祺 + 阿城：合并摘要含两张卡的标题，且只有摘要不含写法示例
    excerpt = genre_style_excerpt("武侠")
    assert "汪曾祺" in excerpt
    assert "阿城" in excerpt
    assert "写法示例" not in excerpt


def test_style_excerpt_unmapped_genre_falls_back_to_default():
    # 映射不上的题材（含空串）回退契诃夫 + 汪曾祺
    excerpt = genre_style_excerpt("菜谱")
    assert "契诃夫" in excerpt
    assert "汪曾祺" in excerpt
    assert genre_style_excerpt("") == genre_style_excerpt("菜谱")


def test_style_excerpt_merged_within_800_chars_for_all_genres():
    # 所有注册题材（含缺省回退）的合并摘要都不超过 800 字
    for keywords, _pack_key in GENRE_SKILL_MAP:
        excerpt = genre_style_excerpt(keywords[0])
        assert excerpt
        assert len(excerpt) <= STYLE_EXCERPT_MAX_CHARS
    assert len(genre_style_excerpt("未登记题材")) <= STYLE_EXCERPT_MAX_CHARS


def test_style_excerpt_digest_format_lines():
    # 摘要逐条为「- 手法名：何时用…」条目，卡标题单独成行
    excerpt = genre_style_excerpt("仙侠")
    lines = [line for line in excerpt.splitlines() if line.strip()]
    assert any(line.startswith("- ") for line in lines)
    assert any(not line.startswith("- ") for line in lines)  # 卡标题行


def test_style_excerpt_missing_cards_dir_returns_empty(tmp_path):
    # 卡目录整体缺失：安静返回空串，不拖垮提示词链路
    assert genre_style_excerpt("武侠", skills_dir=str(tmp_path)) == ""


def test_all_style_cards_frontmatter_parseable():
    # packs/skills/style-*/SKILL.md 批量校验：frontmatter 可解析且 name/description 存在
    from proseforge.application.plugins.skill_import import parse_frontmatter

    cards = sorted(Path(DEFAULT_SKILLS_DIR).glob("style-*/SKILL.md"))
    assert len(cards) == 60
    for card in cards:
        meta, body = parse_frontmatter(card.read_text(encoding="utf-8"))
        assert meta.get("name"), f"{card} 缺 name"
        assert meta.get("description"), f"{card} 缺 description"
        assert body.strip(), f"{card} 正文为空"


def test_memoir_genre_registered_and_pack_loads():
    # memoir 题材包注册：自传/回忆录 → memoir-fiction-writing，摘录非空
    assert skill_key_for_genre("自传") == "memoir-fiction-writing"
    assert skill_key_for_genre("回忆录") == "memoir-fiction-writing"
    excerpt = genre_skill_excerpt("自传")
    assert excerpt
    assert "自传" in excerpt or "回忆录" in excerpt
    # GENRE_STYLE_MAP 配 史铁生/门罗/马尔克斯
    style_excerpt = genre_style_excerpt("自传")
    assert "史铁生" in style_excerpt
    assert len(style_excerpt) <= STYLE_EXCERPT_MAX_CHARS


def test_review_user_prompt_renders_style_block():
    # review_handlers 的 style_block 注入点：非空即进入评审 user prompt
    from proseforge.application.agents.review_handlers import _review_user_prompt

    run = {"id": "run-1", "goal_hash": "g" * 64}
    prompt = _review_user_prompt("style_editor", "review_style", run, [], style_block="【文风技法卡】X")
    assert "【文风技法卡】X" in prompt
    prompt_without = _review_user_prompt("style_editor", "review_style", run, [])
    assert "【文风技法卡】" not in prompt_without
