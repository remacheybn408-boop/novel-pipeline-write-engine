"""人格文件加载（prompts.persona_for_role / prompt_for_role）：文件优先、mtime 缓存、缺失回退。"""

from __future__ import annotations

import os

from proseforge.application.agents import prompts
from proseforge.application.agents.prompts import (
    DEFAULT_PERSONAS_DIR,
    JSON_OUTPUT_INSTRUCTION,
    ROLE_OUTPUT_HINTS,
    ROLE_PROMPTS,
    persona_for_role,
    prompt_for_role,
)
from proseforge.settings import get_settings


def test_persona_for_role_loads_file(tmp_path):
    (tmp_path / "scene_writer.md").write_text("# 人格\n你是场景写手。", encoding="utf-8")
    assert persona_for_role("scene_writer", personas_dir=str(tmp_path)) == "# 人格\n你是场景写手。"


def test_persona_for_role_missing_file_returns_none(tmp_path):
    assert persona_for_role("scene_writer", personas_dir=str(tmp_path)) is None
    assert persona_for_role("unknown_role", personas_dir=str(tmp_path)) is None


def test_persona_for_role_empty_file_returns_none(tmp_path):
    (tmp_path / "analyst.md").write_text("  \n", encoding="utf-8")
    assert persona_for_role("analyst", personas_dir=str(tmp_path)) is None


def test_persona_cache_invalidates_on_mtime_change(tmp_path):
    path = tmp_path / "analyst.md"
    path.write_text("v1 人格", encoding="utf-8")
    assert persona_for_role("analyst", personas_dir=str(tmp_path)) == "v1 人格"
    path.write_text("v2 人格", encoding="utf-8")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 10))
    assert persona_for_role("analyst", personas_dir=str(tmp_path)) == "v2 人格"


def test_prompt_for_role_prefers_persona_file(tmp_path, monkeypatch):
    (tmp_path / "analyst.md").write_text("人格主体：拆解秘书。", encoding="utf-8")
    monkeypatch.setenv("PROSEFORGE_PERSONAS_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        prompt = prompt_for_role("analyst")
    finally:
        get_settings.cache_clear()
    assert prompt.startswith("人格主体：拆解秘书。")
    assert f"输出 {ROLE_OUTPUT_HINTS['analyst']}" in prompt  # hint 拼接保留
    assert prompt.endswith(JSON_OUTPUT_INSTRUCTION)


def test_prompt_for_role_falls_back_to_builtin_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("PROSEFORGE_PERSONAS_DIR", str(tmp_path))  # 目录存在但无人格文件
    get_settings.cache_clear()
    try:
        assert prompt_for_role("scene_writer").startswith(ROLE_PROMPTS["scene_writer"])
        assert prompt_for_role("unknown_role").startswith(prompts.DEFAULT_PROMPT)
    finally:
        get_settings.cache_clear()


def test_bundled_personas_cover_all_roles():
    from pathlib import Path

    root = Path(DEFAULT_PERSONAS_DIR)
    missing = [role for role in ROLE_PROMPTS if not (root / f"{role}.md").is_file()]
    assert missing == []
    for role in ROLE_PROMPTS:
        assert persona_for_role(role, personas_dir=str(root))


def test_bundled_analyst_persona_keeps_role_identity():
    # tests/agents/test_role_handlers.py 依赖 analyst 系统提示词包含“分析 Agent”。
    prompt = prompt_for_role("analyst")
    assert "分析 Agent" in prompt


# ---------------------------------------------------------------------------
# prompt_for_task：task_key 专属人格覆盖角色人格（scene_d 人味写作席位）
# ---------------------------------------------------------------------------


def test_prompt_for_task_prefers_task_persona(tmp_path, monkeypatch):
    from proseforge.application.agents.prompts import prompt_for_task

    (tmp_path / "scene_writer.md").write_text("角色人格：场景写手。", encoding="utf-8")
    (tmp_path / "scene_d.md").write_text("任务人格：人味写作。", encoding="utf-8")
    monkeypatch.setenv("PROSEFORGE_PERSONAS_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        prompt = prompt_for_task("scene_writer", "scene_d")
    finally:
        get_settings.cache_clear()
    assert prompt.startswith("任务人格：人味写作。")
    # 输出形态仍按角色取（JSON 契约是角色级的，不随席位变）。
    assert f"输出 {ROLE_OUTPUT_HINTS['scene_writer']}" in prompt
    assert prompt.endswith(JSON_OUTPUT_INSTRUCTION)


def test_prompt_for_task_falls_back_to_role_persona(tmp_path, monkeypatch):
    from proseforge.application.agents.prompts import prompt_for_task

    (tmp_path / "scene_writer.md").write_text("角色人格：场景写手。", encoding="utf-8")
    monkeypatch.setenv("PROSEFORGE_PERSONAS_DIR", str(tmp_path))  # 无 scene_a.md
    get_settings.cache_clear()
    try:
        prompt = prompt_for_task("scene_writer", "scene_a")
    finally:
        get_settings.cache_clear()
    assert prompt.startswith("角色人格：场景写手。")


def test_bundled_scene_d_persona_wired():
    """仓库自带的 scene_d 人格必须能被 prompt_for_task 命中（人味写作席位）。"""
    from proseforge.application.agents.prompts import prompt_for_task

    prompt = prompt_for_task("scene_writer", "scene_d")
    assert "人味写作" in prompt
    assert f"输出 {ROLE_OUTPUT_HINTS['scene_writer']}" in prompt


# ---------------------------------------------------------------------------
# prompt_for_task：TASK_OUTPUT_HINTS（select 融合定稿形态 ≠ merge_editor 四桶形态）
# ---------------------------------------------------------------------------


def test_prompt_for_task_select_uses_task_output_hint(tmp_path, monkeypatch):
    from proseforge.application.agents.prompts import TASK_OUTPUT_HINTS, prompt_for_task

    (tmp_path / "select.md").write_text("任务人格：融合编辑。", encoding="utf-8")
    monkeypatch.setenv("PROSEFORGE_PERSONAS_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        prompt = prompt_for_task("merge_editor", "select")
    finally:
        get_settings.cache_clear()
    assert prompt.startswith("任务人格：融合编辑。")
    # 输出形态按任务取（融合定稿），不是 merge_editor 的四桶分类形态。
    assert f"输出 {TASK_OUTPUT_HINTS['select']}" in prompt
    assert "agreements" not in prompt


def test_bundled_select_persona_wired():
    """仓库自带的 select 人格（融合编辑席位）必须能被 prompt_for_task 命中。"""
    from proseforge.application.agents.prompts import prompt_for_task

    prompt = prompt_for_task("merge_editor", "select")
    assert "融合编辑" in prompt
    assert '"content"' in prompt  # 融合定稿形态（title/content/rationale/backbone/sources）
