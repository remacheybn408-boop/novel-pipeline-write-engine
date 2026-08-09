from fastapi.testclient import TestClient

from proseforge.api.main import create_app


def test_project_routes_require_authentication():
    response = TestClient(create_app()).get("/api/v1/projects/my-book")
    assert response.status_code == 401


def test_project_response_does_not_expose_owner_id():
    assert "owner_id" not in {"id", "slug", "title", "genre", "style", "language", "status"}
