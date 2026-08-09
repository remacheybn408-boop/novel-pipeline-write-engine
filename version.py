"""version.py — Single source of truth for engine version."""
from pathlib import Path

_VFILE = Path(__file__).resolve().parent / "VERSION"

def get_version() -> str:
    """Return version string from VERSION file."""
    try:
        return _VFILE.read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"

__version__ = get_version()
