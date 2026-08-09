"""V15-002：GET /api/v1/runtime/info 端点契约测试。"""

from fastapi.testclient import TestClient

from proseforge.api.main import create_app

EXPECTED_KEYS = {
    "version",
    "profile",
    "platform",
    "database",
    "queue",
    "web_served_by",
    "data_dir_is_absolute",
}


def test_runtime_info_returns_200_with_seven_keys():
    response = TestClient(create_app()).get("/api/v1/runtime/info")
    assert response.status_code == 200
    assert set(response.json()) == EXPECTED_KEYS


def test_runtime_info_values_match_runtime_state():
    app = create_app()
    payload = TestClient(app).get("/api/v1/runtime/info").json()
    assert payload == app.state.runtime.info
    assert payload["profile"] == app.state.runtime.profile.value


def test_runtime_info_never_leaks_absolute_paths_or_credentials():
    payload = TestClient(create_app()).get("/api/v1/runtime/info").json()
    for key in payload:
        lowered_key = key.lower()
        assert "key" not in lowered_key
        assert "secret" not in lowered_key
    for value in payload.values():
        if isinstance(value, str):
            assert "/" not in value
            assert "\\" not in value
            assert "@" not in value
            assert "postgres:" not in value.lower()
            assert "redis:" not in value.lower()
