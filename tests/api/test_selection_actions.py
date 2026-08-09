from __future__ import annotations

import hashlib
import uuid

import pytest


@pytest.fixture()
def chapter_with_active_version(auth_client):
    project = auth_client.post_json(
        "/api/v1/projects",
        {"slug": f"selection-{uuid.uuid4().hex[:12]}", "title": "Selection actions"},
    ).json()
    chapter = auth_client.post_json(
        f"/api/v1/projects/{project['id']}/chapters",
        {"chapter_no": 1, "title": "Opening"},
    ).json()
    content = "Mira enters the harbor at dawn."
    version = auth_client.post_json(
        f"/api/v1/chapters/{chapter['id']}/versions", {"content": content}
    ).json()
    return chapter, version, content


def _payload(version: dict[str, object], content: str, *, action: str = "rewrite", params: dict[str, object] | None = None):
    start, end = 5, 11
    return {
        "action": action,
        "from": start,
        "to": end,
        "selected_text_hash": hashlib.sha256(content[start:end].encode("utf-8")).hexdigest(),
        "base_version_id": version["id"],
        "params": params or {},
    }


def test_selection_action_creates_proposal_without_changing_active_chapter(auth_client, chapter_with_active_version):
    chapter, version, content = chapter_with_active_version
    before_versions = auth_client.get(f"/api/v1/chapters/{chapter['id']}/versions").json()
    before_proposals = auth_client.get(f"/api/v2/chapters/{chapter['id']}/proposals").json()

    response = auth_client.post_json(
        f"/api/v2/chapters/{chapter['id']}/selection-actions", _payload(version, content)
    )

    assert response.status_code == 201
    assert response.json()["proposal_id"]
    assert auth_client.get(f"/api/v1/chapters/{chapter['id']}/versions").json() == before_versions
    after_proposals = auth_client.get(f"/api/v2/chapters/{chapter['id']}/proposals").json()
    assert len(after_proposals) == len(before_proposals) + 1
    assert after_proposals[-1]["base_version_id"] == version["id"]


def test_continue_creates_one_proposal_per_requested_candidate(auth_client, chapter_with_active_version):
    chapter, version, content = chapter_with_active_version

    response = auth_client.post_json(
        f"/api/v2/chapters/{chapter['id']}/selection-actions",
        _payload(version, content, action="continue", params={"candidates": 3}),
    )

    assert response.status_code == 201
    assert len(response.json()["candidate_proposal_ids"]) == 3
    proposals = auth_client.get(f"/api/v2/chapters/{chapter['id']}/proposals").json()
    assert len(proposals) == 3


def test_review_selection_creates_a_report_not_a_proposal(auth_client, chapter_with_active_version):
    chapter, version, content = chapter_with_active_version
    response = auth_client.post_json(
        f"/api/v2/chapters/{chapter['id']}/selection-actions", _payload(version, content, action="review")
    )
    assert response.status_code == 201
    assert response.json()["review_id"]
    assert auth_client.get(f"/api/v2/chapters/{chapter['id']}/proposals").json() == []


@pytest.mark.parametrize("conflict", ["hash", "base"])
def test_selection_action_rejects_stale_hash_or_base_version(auth_client, chapter_with_active_version, conflict):
    chapter, version, content = chapter_with_active_version
    payload = _payload(version, content)
    if conflict == "hash":
        payload["selected_text_hash"] = hashlib.sha256(b"different").hexdigest()
    else:
        payload["base_version_id"] = "stale-version-id"

    response = auth_client.post_json(f"/api/v2/chapters/{chapter['id']}/selection-actions", payload)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] in {"SELECTION_HASH_CONFLICT", "BASE_VERSION_CONFLICT"}
    assert auth_client.get(f"/api/v2/chapters/{chapter['id']}/proposals").json() == []
