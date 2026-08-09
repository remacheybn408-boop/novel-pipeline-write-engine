"""Skill 上传文件解析（.md / .zip + 极简 YAML frontmatter）。

纯 stdlib 实现（zipfile + 手写 frontmatter 解析，不引 pyyaml），供
api/routes/plugins.py 的 upload 端点与离线冒烟复用。解析规则：
- zip：优先 SKILL.md（任意目录层级、大小写不敏感），找不到取第一个 .md；
- frontmatter：文件以 ``---`` 行开头时，到下一个 ``---`` 行之间的块里
  只识别 ``name:`` / ``description:`` 两键；块存在但两键都缺或行格式非法
  → ValueError（路由层转 422）；
- content = frontmatter 之后的正文（无 frontmatter 则全文）。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedSkill:
    name: str
    description: str
    content: str


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析 --- 包围的 frontmatter；返回 (meta, body)。无块时 ({}, 全文)。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        raise ValueError("frontmatter block is not closed")
    meta: dict[str, str] = {}
    for line in lines[1:closing]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise ValueError(f"invalid frontmatter line: {stripped[:50]}")
        key, _, value = stripped.partition(":")
        key = key.strip().lower()
        if key in {"name", "description"}:
            meta[key] = value.strip().strip('"').strip("'")
    body = "\n".join(lines[closing + 1:]).strip()
    return meta, body


def parse_skill_markdown(filename: str, text: str) -> ParsedSkill:
    """md 文本 → ParsedSkill；name 缺省回退文件名主干，content 为空 → ValueError。"""
    meta, body = parse_frontmatter(text)
    name = meta.get("name") or filename.rsplit("/", 1)[-1].rsplit(".", 1)[0].strip()
    if not name:
        raise ValueError("skill name is required (frontmatter name or filename)")
    content = body or text.strip()
    if not content:
        raise ValueError("skill content is empty")
    return ParsedSkill(name=name, description=meta.get("description", ""), content=content)


def extract_skill_from_archive(filename: str, data: bytes) -> tuple[str, str]:
    """(.md|.zip) 字节流 → (内部文件名, 文本)。zip 内优先 SKILL.md，其次第一个 .md。"""
    lowered = filename.lower()
    if lowered.endswith(".md"):
        return filename, data.decode("utf-8")
    if not lowered.endswith(".zip"):
        raise ValueError("only .md or .zip files are supported")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        candidates = [name for name in archive.namelist() if not name.endswith("/") and name.lower().endswith(".md")]
        if not candidates:
            raise ValueError("zip archive contains no markdown file")
        chosen = next((name for name in candidates if name.rsplit("/", 1)[-1].lower() == "skill.md"), candidates[0])
        return chosen, archive.read(chosen).decode("utf-8")
