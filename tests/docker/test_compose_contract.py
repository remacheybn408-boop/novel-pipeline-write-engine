from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))


def test_required_services_exist() -> None:
    services = load_compose()["services"]
    assert {"web", "api", "worker", "scheduler", "postgres", "redis"} <= set(services)


def test_required_named_volumes_exist() -> None:
    volumes = load_compose()["volumes"]
    assert {
        "postgres-data",
        "proseforge-blobs",
        "proseforge-backups",
        "redis-data",
    } <= set(volumes)
