from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN_DOMAIN_PREFIXES = {
    "fastapi",
    "sqlalchemy",
    "celery",
    "redis",
    "httpx",
    "argparse",
}


def imports_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_domain_does_not_import_infrastructure_frameworks() -> None:
    domain = ROOT / "proseforge" / "domain"
    assert domain.exists(), "proseforge/domain must be created"
    violations: list[str] = []
    for path in domain.rglob("*.py"):
        for name in imports_in(path):
            if any(
                name == prefix or name.startswith(prefix + ".")
                for prefix in FORBIDDEN_DOMAIN_PREFIXES
            ):
                violations.append(f"{path.relative_to(ROOT)} imports {name}")
    assert violations == []


def test_no_module_imports_src() -> None:
    # The legacy desktop src/ tree was removed in v1.5 (see CHANGELOG
    # Unreleased); nothing in the web package may import it.
    package = ROOT / "proseforge"
    assert package.exists(), "proseforge package must be created"
    violations: list[str] = []
    for path in package.rglob("*.py"):
        for name in imports_in(path):
            if name == "src" or name.startswith("src."):
                violations.append(f"{path.relative_to(ROOT)} imports {name}")
    assert violations == []
