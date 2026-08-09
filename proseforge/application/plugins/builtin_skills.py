"""内置 skill 目录加载器（packs/skills）。

每个含 SKILL.md 的子目录产出一个 BuiltinSkill：
- ``skill_key`` = 目录名；
- ``name`` = 正文首个 ``# `` H1（无 H1 回退 frontmatter name，再回退目录名）；
- ``description`` = frontmatter description；
- ``content`` = frontmatter 之后的正文；
- ``category`` = ``fiction``（*-fiction-writing 类型包）或 ``tool``（craft-*
  技法包与系统包），供前端分组渲染。

frontmatter 复用 skill_import 的解析。进程级缓存按目录内 SKILL.md 的
最新 mtime 失效；目录不存在返回空列表（不报错）。agents/ 与 references/
子目录本期不消费，原样保留在磁盘即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from proseforge.application.plugins.skill_import import parse_frontmatter

DEFAULT_SKILLS_DIR = "packs/skills"

# 内置 skill 分类：类型包（*-fiction-writing）= 小说类；craft-* 技法包与
# 系统包（如 builtin-narrative-rag）= 工具类。wire 值是英文 key，中文标签
# 由前端渲染。
CATEGORY_FICTION = "fiction"
CATEGORY_TOOL = "tool"


def category_for_key(skill_key: str) -> str:
    return CATEGORY_FICTION if skill_key.endswith("-fiction-writing") else CATEGORY_TOOL


@dataclass(frozen=True)
class BuiltinSkill:
    skill_key: str
    name: str
    description: str
    content: str
    category: str


# skills_dir → (最新 SKILL.md mtime, 解析结果)；mtime 变化即重新扫描
_cache: dict[str, tuple[float, list[BuiltinSkill]]] = {}


def _first_h1(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _load(skill_dir: Path, skills_dir: str) -> BuiltinSkill | None:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        meta, body = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None  # 单个包损坏不拖垮整个目录
    content = body.strip()
    if not content:
        return None
    name = _first_h1(body) or meta.get("name") or skill_dir.name
    return BuiltinSkill(
        skill_key=skill_dir.name,
        name=name,
        description=meta.get("description", ""),
        content=content,
        category=category_for_key(skill_dir.name),
    )


def load_builtin_skills(skills_dir: str = DEFAULT_SKILLS_DIR) -> list[BuiltinSkill]:
    """扫描 skills 目录（相对 CWD），返回按 skill_key 排序的内置 skill 列表。"""
    root = Path(skills_dir)
    if not root.is_dir():
        return []
    marker = max((path.stat().st_mtime for path in root.glob("*/SKILL.md")), default=0.0)
    cached = _cache.get(skills_dir)
    if cached is not None and cached[0] == marker:
        return cached[1]
    skills = sorted(
        (skill for child in sorted(root.iterdir()) if child.is_dir() for skill in [_load(child, skills_dir)] if skill is not None),
        key=lambda skill: skill.skill_key,
    )
    _cache[skills_dir] = (marker, skills)
    return skills
