from fastapi.testclient import TestClient

from proseforge.api.main import create_app


def test_usage_routes_require_authentication():
    client = TestClient(create_app())

    assert client.get("/api/v1/usage/summary").status_code == 401
    assert client.get("/api/v1/usage/records").status_code == 401
    assert client.get("/api/v1/usage/by-model").status_code == 401
