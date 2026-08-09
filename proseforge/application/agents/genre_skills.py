"""题材技能包注入（packs/skills/<genre-pack>/SKILL.md → 写作/审校提示词）。

projects.genre 是中文自由文本，batch_dispatch.chapter_goal 把它拼成 goal 里的一行
「题材：X」。本模块负责三段链路：goal 文本解析题材行 → 中文题材关键词映射到
题材包目录 → 读取 SKILL.md 正文（去 frontmatter、截断）供提示词注入。

映射不上的题材、缺失/损坏的包一律返回空串（安静跳过，不拖垮提示词链路）。
mtime 缓存仿 prompts.persona_for_role：文件变更即重新读取。
"""

from __future__ import annotations

import re
from pathlib import Path

from proseforge.application.plugins.skill_import import parse_frontmatter

DEFAULT_SKILLS_DIR = "packs/skills"

# 中文题材关键词 → packs/skills 下的题材包目录名；按序匹配，先中先生效。
# 顺序即优先级：更具体的情感类与幻想类排在泛化词（架空/都市）之前。
GENRE_SKILL_MAP: tuple[tuple[tuple[str, ...], str], ...] = (
    (("古言", "宫斗", "宅斗"), "ancient-romance-fiction-writing"),
    (("末日", "末世", "废土"), "apocalypse-fiction-writing"),
    (("校园",), "campus-fiction-writing"),
    (("克苏鲁",), "cthulhu-fiction-writing"),
    (("赛博朋克", "赛博"), "cyberpunk-fiction-writing"),
    (("耽美", "纯爱"), "danmei-fiction-writing"),
    (("玄幻", "东方幻想"), "eastern-fantasy-fiction-writing"),
    (("游戏", "网游", "电竞"), "game-fiction-writing"),
    (("无限流",), "infinite-flow-fiction-writing"),
    (("轻小说",), "light-novel-fiction-writing"),
    (("自传", "回忆录", "传记"), "memoir-fiction-writing"),
    (("军事", "军旅", "战争"), "military-fiction-writing"),
    (("悬疑", "推理", "侦探"), "mystery-fiction-writing"),
    (("言情", "爱情", "情感", "恋爱", "甜宠"), "romance-fiction-writing"),
    (("科幻",), "science-fiction-writing"),
    (("灵异", "恐怖", "惊悚", "鬼怪"), "supernatural-fiction-writing"),
    (("西幻", "奇幻", "魔幻"), "western-fantasy-fiction-writing"),
    (("职场",), "workplace-fiction-writing"),
    (("武侠",), "wuxia-fiction-writing"),
    (("仙侠", "修真", "修仙"), "xianxia-fiction-writing"),
    (("历史", "架空"), "historical-fiction-writing"),
    (("都市",), "urban-fiction-writing"),
)

_GENRE_LINE = re.compile(r"^题材[:：]\s*(?P<genre>.+?)\s*$", re.MULTILINE)

# 题材包目录名 → 作家文风技法卡（packs/skills/style-<slug>）：每题材 2-3 张，
# 按题材气质搭配，作为行文/审校的审美基准注入。映射不上的题材回退
# DEFAULT_STYLE_SLUGS（契诃夫 + 汪曾祺：克制白描的通用底座）。
GENRE_STYLE_MAP: dict[str, tuple[str, ...]] = {
    "ancient-romance-fiction-writing": ("style-zhang-ailing", "style-wang-zengqi"),
    "apocalypse-fiction-writing": ("style-albert-camus", "style-abdulrazak-gurnah"),
    "campus-fiction-writing": ("style-cho-namjoo", "style-isaka-kotaro"),
    "cthulhu-fiction-writing": ("style-borges", "style-bulgakov"),
    "cyberpunk-fiction-writing": ("style-bulgakov", "style-murakami-haruki"),
    "danmei-fiction-writing": ("style-zhang-ailing", "style-miura-shion"),
    "eastern-fantasy-fiction-writing": ("style-mo-yan", "style-garcia-marquez"),
    "game-fiction-writing": ("style-isaka-kotaro", "style-miura-shion"),
    "historical-fiction-writing": ("style-chen-zhongshi", "style-ma-boyong"),
    "infinite-flow-fiction-writing": ("style-julio-cortazar", "style-higashino-keigo"),
    "light-novel-fiction-writing": ("style-yoshimoto-banana", "style-isaka-kotaro"),
    "literary-fiction-writing": ("style-alice-munro", "style-anton-chekhov"),
    "memoir-fiction-writing": ("style-shi-tiesheng", "style-alice-munro", "style-garcia-marquez"),
    "military-fiction-writing": ("style-leo-tolstoy", "style-john-steinbeck"),
    "mystery-fiction-writing": ("style-higashino-keigo", "style-kazuo-ishiguro"),
    "romance-fiction-writing": ("style-zhang-ailing", "style-yoshimoto-banana"),
    "science-fiction-writing": ("style-murakami-haruki", "style-bulgakov"),
    "supernatural-fiction-writing": ("style-akutagawa-ryunosuke", "style-bulgakov"),
    "urban-fiction-writing": ("style-liu-zhenyun", "style-celeste-ng"),
    "western-fantasy-fiction-writing": ("style-leo-tolstoy", "style-victor-hugo"),
    "workplace-fiction-writing": ("style-cho-namjoo", "style-anne-tyler"),
    "wuxia-fiction-writing": ("style-wang-zengqi", "style-a-cheng"),
    "xianxia-fiction-writing": ("style-a-cheng", "style-kawabata-yasunari"),
}

DEFAULT_STYLE_SLUGS: tuple[str, ...] = ("style-anton-chekhov", "style-wang-zengqi")

# 技法卡合并摘录的体积上限：只取核心手法标题 + 「何时用」级摘要，不塞全文。
STYLE_EXCERPT_MAX_CHARS = 800

# SKILL.md 路径 → (文件 mtime, 去 frontmatter 的正文)；mtime 变化即重新读取
_skill_cache: dict[str, tuple[float, str]] = {}


def genre_from_goal(goal: str) -> str:
    """从 run goal 文本解析「题材：X」行；无此行返回空串。"""
    match = _GENRE_LINE.search(goal or "")
    return match.group("genre") if match else ""


def skill_key_for_genre(genre: str) -> str:
    """中文题材（自由文本）→ 题材包目录名；映射不上返回空串。"""
    genre = (genre or "").strip()
    if not genre:
        return ""
    for keywords, pack_key in GENRE_SKILL_MAP:
        if any(word in genre for word in keywords):
            return pack_key
    return ""


def _skills_dir() -> str:
    """技能包目录：从 settings 读（与 personas_dir 同款机制），惰性 import 避免循环依赖。"""
    from proseforge.settings import get_settings

    return get_settings().skills_dir


def _skill_body(pack_key: str, skills_dir: str) -> str:
    """读取题材包 SKILL.md 正文（去 frontmatter）；缺失/损坏/为空返回空串。"""
    path = Path(skills_dir) / pack_key / "SKILL.md"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    cache_key = str(path)
    cached = _skill_cache.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        _meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return ""  # 单个包损坏不拖垮整个提示词链路
    text = body.strip()
    if not text:
        return ""
    _skill_cache[cache_key] = (mtime, text)
    return text


def genre_skill_excerpt(genre: str, max_chars: int = 1200, *, skills_dir: str | None = None) -> str:
    """题材 → SKILL.md 正文摘录（截断到 max_chars）；无映射/无包返回空串。"""
    pack_key = skill_key_for_genre(genre)
    if not pack_key:
        return ""
    body = _skill_body(pack_key, skills_dir or _skills_dir())
    if not body:
        return ""
    if len(body) > max_chars:
        body = body[: max_chars - 1] + "…"
    return body


def style_slugs_for_genre(genre: str) -> tuple[str, ...]:
    """题材 → 文风技法卡 slug 列表；无题材/无映射回退 DEFAULT_STYLE_SLUGS。"""
    return GENRE_STYLE_MAP.get(skill_key_for_genre(genre), DEFAULT_STYLE_SLUGS)


_STYLE_ITEM = re.compile(r"^\s*\d+\.\s*\*\*(?P<title>.+?)\*\*[:：](?P<rest>.+)$")
_WHEN_TO_USE = re.compile(r"何时用[:：](?P<when>[^。\n]+)")


def _style_card_digest(body: str) -> str:
    """单张技法卡 → 核心手法摘要：卡标题 + 各手法「标题：何时用」，不含写法示例。"""
    if not body:
        return ""
    title = ""
    items: list[str] = []
    in_core = False
    for line in body.splitlines():
        stripped = line.strip()
        if not title and stripped.startswith("# "):
            title = stripped[2:].strip()
        if stripped.startswith("## "):
            in_core = "核心手法" in stripped
            continue
        if not in_core:
            continue
        match = _STYLE_ITEM.match(stripped)
        if not match:
            continue
        summary = match.group("title").strip()
        when = _WHEN_TO_USE.search(match.group("rest"))
        if when:
            summary += f"：{when.group('when').strip()}"
        items.append(summary)
    if not items:
        # 结构异常卡：退化为正文开头摘要，不拖垮注入链路
        return body[:200]
    return (title or "技法卡") + "\n" + "\n".join(f"- {item}" for item in items)


def genre_style_excerpt(genre: str, max_chars: int = STYLE_EXCERPT_MAX_CHARS, *, skills_dir: str | None = None) -> str:
    """题材 → 2-3 张文风技法卡的合并摘要（≤max_chars）；全部缺失返回空串。"""
    directory = skills_dir or _skills_dir()
    digests = [digest for slug in style_slugs_for_genre(genre) if (digest := _style_card_digest(_skill_body(slug, directory)))]
    if not digests:
        return ""
    merged = "\n\n".join(digests)
    if len(merged) > max_chars:
        merged = merged[: max_chars - 1] + "…"
    return merged
