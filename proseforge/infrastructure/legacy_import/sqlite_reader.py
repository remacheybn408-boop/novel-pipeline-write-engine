from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LegacyChapter:
    chapter_no: int
    title: str
    versions: tuple[str, ...]

    @property
    def latest_hash(self) -> str:
        return hashlib.sha256(self.versions[-1].encode("utf-8")).hexdigest() if self.versions else ""


@dataclass(frozen=True)
class LegacySnapshot:
    chapters: tuple[LegacyChapter, ...]
    outline_count: int
    artifact_hashes: tuple[str, ...]


def read_legacy_slot(slot_root: Path, database: Path) -> LegacySnapshot:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        chapters: list[LegacyChapter] = []
        if "chapters" in tables:
            chapter_rows = connection.execute("SELECT id, chapter_no, title FROM chapters ORDER BY chapter_no").fetchall()
            for chapter_id, number, title in chapter_rows:
                versions: list[str] = []
                if "chapter_versions" in tables:
                    rows = connection.execute("SELECT content FROM chapter_versions WHERE chapter_id = ? ORDER BY version_no", (chapter_id,)).fetchall()
                    versions = [str(row[0]) for row in rows]
                chapters.append(LegacyChapter(int(number), str(title), tuple(versions)))
    outline_count = len(list((slot_root / "outlines").glob("*.json")))
    artifacts = []
    for path in sorted((slot_root / "reports").glob("**/*")) if (slot_root / "reports").exists() else []:
        if path.is_file():
            artifacts.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return LegacySnapshot(tuple(chapters), outline_count, tuple(artifacts))
