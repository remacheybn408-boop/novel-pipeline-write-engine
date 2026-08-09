from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_sources_have_no_direct_plugin_imports() -> None:
    forbidden = ("proseforge-codex", "proseforge-Hermes", "proseforge-claude")
    violations: list[str] = []
    for source_root in (ROOT / "proseforge", ROOT / "src"):
        for path in source_root.rglob("*.py"):
            content = path.read_text(encoding="utf-8")
            for marker in forbidden:
                if marker in content:
                    violations.append(f"{path.relative_to(ROOT)} contains {marker}")
    assert violations == []
