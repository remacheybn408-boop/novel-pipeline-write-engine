from __future__ import annotations

import asyncio
import uuid

from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

ORIGIN = "http://testserver"


def _create_project(auth_client) -> str:
    response = auth_client.post_json(
        "/api/v1/projects",
        {"slug": f"story-bible-{uuid.uuid4().hex[:12]}", "title": "Story Bible Novel"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _create_fact(auth_client, project_id: str, *, kind: str = "character", key: str = "Mira", value: dict | None = None, pinned: bool = False) -> dict:
    response = auth_client.post_json(
        f"/api/v2/projects/{project_id}/story-bible/entries",
        {
            "kind": kind,
            "key": key,
            "value": value or {"triggers": [key], "budget_tokens": 24},
            "pinned": pinned,
        },
    )
    assert response.status_code == 201
    return response.json()


def _other_user_headers(auth_client, client, api_settings) -> dict[str, str]:
    other_email = f"story-bible-{uuid.uuid4().hex[:8]}@example.local"

    async def create_other_user() -> None:
        from proseforge.infrastructure.database.session import (
            create_engine_and_sessionmaker,
        )

        engine, factory = create_engine_and_sessionmaker(api_settings)
        try:
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.users.create(other_email, client.app.state.auth.hash_password("twelve-char-pw"), "USER")
                await uow.commit()
        finally:
            await engine.dispose()

    asyncio.run(create_other_user())
    login = auth_client.raw.post(
        "/api/v1/auth/login",
        json={"email": other_email, "password": "twelve-char-pw"},
        headers={"Origin": ORIGIN},
    )
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}", "Origin": ORIGIN}


def test_authenticated_user_can_create_and_list_story_bible_entries(auth_client):
    project_id = _create_project(auth_client)

    created = _create_fact(auth_client, project_id)
    listed = auth_client.get(f"/api/v2/projects/{project_id}/story-bible")

    assert listed.status_code == 200
    assert listed.json() == [created]
    assert created["project_id"] == project_id
    assert created["kind"] == "character"
    assert created["key"] == "Mira"
    assert created["value"] == {"triggers": ["Mira"], "budget_tokens": 24}
    assert created["version"] == 1


def test_story_bible_entries_return_404_to_foreign_user(auth_client, client, api_settings):
    project_id = _create_project(auth_client)
    entry = _create_fact(auth_client, project_id)
    other_headers = _other_user_headers(auth_client, client, api_settings)

    assert auth_client.get(f"/api/v2/projects/{project_id}/story-bible", headers=other_headers).status_code == 404
    assert auth_client.raw.post(
        f"/api/v2/story-bible/{entry['id']}/pin",
        headers=other_headers,
    ).status_code == 404


def test_story_bible_patch_rejects_stale_expected_version(auth_client):
    project_id = _create_project(auth_client)
    entry = _create_fact(auth_client, project_id)
    patch_url = f"/api/v2/story-bible/{entry['id']}"

    updated = auth_client.raw.patch(
        patch_url,
        json={"value": {"triggers": ["Mira", "captain"], "budget_tokens": 32}, "expected_version": 1},
        headers={"Origin": ORIGIN},
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2

    stale = auth_client.raw.patch(
        patch_url,
        json={"pinned": True, "expected_version": 1},
        headers={"Origin": ORIGIN},
    )
    assert stale.status_code == 409


def test_promise_cannot_leave_terminal_state_and_reports_allowed_transitions(auth_client):
    project_id = _create_project(auth_client)
    promise = _create_fact(
        auth_client,
        project_id,
        kind="promise",
        key="the letter",
        value={"triggers": ["letter"], "budget_tokens": 24},
    )
    status_url = f"/api/v2/story-bible/{promise['id']}/status"

    assert auth_client.post_json(status_url, {"status": "developing"}).status_code == 200
    assert auth_client.post_json(status_url, {"status": "resolved"}).status_code == 200

    invalid = auth_client.post_json(status_url, {"status": "open"})

    assert invalid.status_code == 422
    assert invalid.json()["detail"]["details"]["allowed"] == []


def test_context_preview_injects_only_pinned_or_triggered_facts(auth_client):
    project_id = _create_project(auth_client)
    pinned = _create_fact(auth_client, project_id, kind="world_rule", key="canon", value={"triggers": [], "budget_tokens": 24}, pinned=True)
    triggered = _create_fact(auth_client, project_id, key="Mira")
    missed = _create_fact(auth_client, project_id, key="Ilan")

    response = auth_client.post_json(f"/api/v2/projects/{project_id}/context/preview", {"text": "Mira enters the harbor."})

    assert response.status_code == 200
    payload = response.json()["payload"]
    assert payload["injected_fact_ids"] == [pinned["id"], triggered["id"]]
    assert missed["id"] not in [block["source_id"] for block in payload["blocks"]]
    assert any(item["source_id"] == missed["id"] and item["reason"] == "not_triggered" for item in payload["omitted"])
