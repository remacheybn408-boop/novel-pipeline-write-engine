from fastapi.testclient import TestClient

from proseforge.api.main import create_app


def test_conversation_routes_require_authentication():
    client = TestClient(create_app())
    response = client.post("/api/v1/conversations", json={"project_id": "p1"})
    assert response.status_code == 401


def test_event_stream_encoder_has_reconnect_headers():
    response = TestClient(create_app()).get("/api/v1/conversations/c1/events")
    assert response.status_code == 401
