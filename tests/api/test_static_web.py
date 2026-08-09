from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.settings import Settings


def test_native_web_serves_runtime_config_and_spa_without_exposing_secrets(
    tmp_path: Path,
) -> None:
    frontend = tmp_path / "dist"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<html><body><div id="root">shell</div></body></html>',
        encoding="utf-8",
    )
    (frontend / "assets").mkdir()
    (frontend / "assets" / "app.js").write_text("console.log('ok')", encoding="utf-8")
    settings = Settings(
        runtime_profile="server",
        frontend_dir=str(frontend),
        serve_web=True,
        master_key="replace-with-32-byte-base64-key",
        jwt_secret="replace-with-long-random-secret",
    )

    client = TestClient(create_app(settings))

    config = client.get("/runtime-config.json")
    shell = client.get("/projects/example")
    asset = client.get("/assets/app.js")

    assert config.status_code == 200
    assert config.json() == {"api_base_url": "/api", "profile": "server"}
    assert "replace-with" not in config.text
    assert shell.status_code == 200
    assert "id=\"root\"" in shell.text
    assert asset.status_code == 200
    assert asset.text == "console.log('ok')"


def test_api_paths_are_not_captured_by_spa_fallback(tmp_path: Path) -> None:
    frontend = tmp_path / "dist"
    frontend.mkdir()
    (frontend / "index.html").write_text("shell", encoding="utf-8")
    settings = Settings(
        runtime_profile="server",
        frontend_dir=str(frontend),
        serve_web=True,
    )

    response = TestClient(create_app(settings)).get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
