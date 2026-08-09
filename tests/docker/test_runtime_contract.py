from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_images_drop_root_and_healthchecks_are_present():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    for name in ("api", "worker", "scheduler"):
        dockerfile = (ROOT / services[name]["build"]["dockerfile"]).read_text(encoding="utf-8")
        assert "USER proseforge" in dockerfile
        assert "10001" in dockerfile
        assert "healthcheck" in services[name]


def test_nginx_keeps_sse_unbuffered():
    nginx = (ROOT / "docker" / "nginx.conf").read_text(encoding="utf-8")
    assert "proxy_buffering off;" in nginx
    assert "proxy_cache off;" in nginx
    assert "proxy_read_timeout 3600s;" in nginx
