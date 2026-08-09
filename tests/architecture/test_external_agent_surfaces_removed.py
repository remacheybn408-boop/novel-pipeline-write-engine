from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_external_agent_surfaces_are_removed() -> None:
    forbidden = [
        ROOT / "plugin" / "proseforge-codex",
        ROOT / "plugin" / "proseforge-Hermes",
        ROOT / "plugin" / "proseforge-claude",
        ROOT / ".claude",
        ROOT / "install_plugin.py",
    ]
    assert [str(path.relative_to(ROOT)) for path in forbidden if path.exists()] == []
