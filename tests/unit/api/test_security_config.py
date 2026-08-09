from fastapi.testclient import TestClient

from proseforge.api.main import create_app


def test_logout_rejects_cross_origin_cookie_requests():
    client = TestClient(create_app())

    response = client.post("/api/v1/auth/logout", headers={"origin": "https://evil.example"})

    assert response.status_code == 403
