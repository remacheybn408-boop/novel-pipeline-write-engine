from __future__ import annotations

from proseforge.api.main import create_app


def test_create_app_exposes_runtime_lifecycle_not_ready_before_startup() -> None:
    application = create_app()

    assert application.state.lifecycle.ready is False
