from __future__ import annotations

import uuid

import pytest


@pytest.fixture()
def chapter_version(auth_client):
    project = auth_client.post_json("/api/v1/projects", {"slug": f"review-{uuid.uuid4().hex[:12]}", "title": "Revision test"}).json()
    chapter = auth_client.post_json(f"/api/v1/projects/{project['id']}/chapters", {"chapter_no": 1, "title": "Opening"}).json()
    version = auth_client.post_json(f"/api/v1/chapters/{chapter['id']}/versions", {"content": "one two three"}).json()
    return project, chapter, version


def create_proposal(auth_client, chapter, version, **overrides):
    payload = {"base_version_id": version["id"], "after_text": "ONE TWO THREE", "rationale": "Improve emphasis."}
    payload.update(overrides)
    response = auth_client.post_json(f"/api/v2/chapters/{chapter['id']}/revision-proposals", payload)
    assert response.status_code == 201
    return response.json()


def test_approval_is_idempotent_and_creates_one_version(auth_client, chapter_version):
    _, chapter, version = chapter_version
    proposal = create_proposal(auth_client, chapter, version)
    key = f"approve-{uuid.uuid4().hex}"
    first = auth_client.post(f"/api/v2/revision-proposals/{proposal['id']}/approve", headers={"Idempotency-Key": key})
    replay = auth_client.post(f"/api/v2/revision-proposals/{proposal['id']}/approve", headers={"Idempotency-Key": key})
    assert first.status_code == replay.status_code == 200
    assert first.json()["status"] == replay.json()["status"] == "VERSION_CREATED"
    assert replay.json()["replayed"] is True
    versions = auth_client.get(f"/api/v1/chapters/{chapter['id']}/versions").json()
    assert len(versions) == 2
    assert first.json()["version"]["id"] == replay.json()["version"]["id"]


def test_approval_rejects_stale_base_with_current_version(auth_client, chapter_version):
    _, chapter, version = chapter_version
    proposal = create_proposal(auth_client, chapter, version)
    newer = auth_client.post_json(f"/api/v1/chapters/{chapter['id']}/versions", {"content": "someone else saved", "base_version": version["version_no"]})
    assert newer.status_code == 201
    response = auth_client.post(f"/api/v2/revision-proposals/{proposal['id']}/approve")
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "REVISION_BASE_CONFLICT", "current_version_id": newer.json()["id"]}


def test_partial_hunk_acceptance_preserves_rejected_text(auth_client, chapter_version):
    _, chapter, version = chapter_version
    proposal = create_proposal(
        auth_client, chapter, version, after_text="ONE TWO three",
        hunks=[{"start": 0, "end": 3, "replacement": "ONE"}, {"start": 4, "end": 7, "replacement": "TWO"}],
    )
    response = auth_client.post_json(f"/api/v2/revision-proposals/{proposal['id']}/approve", {"accept_hunks": [0]})
    assert response.status_code == 200
    assert response.json()["version"]["content"] == "ONE two three"


def test_blocking_guard_refuses_approval(auth_client, chapter_version):
    _, chapter, version = chapter_version
    proposal = create_proposal(auth_client, chapter, version, guard_status="blocked")
    response = auth_client.post(f"/api/v2/revision-proposals/{proposal['id']}/approve")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "REVISION_GUARD_BLOCKED"


def test_review_report_persists_findings_and_evidence(auth_client, chapter_version):
    project, chapter, _ = chapter_version
    payload = {"scope": "chapter", "subject_type": "chapter", "subject_id": chapter["id"], "findings": [{"severity": "blocking", "message": "Timeline breaks.", "evidence": [{"from": 2, "to": 5}]}], "scores": {"continuity": 0.2}, "model_snapshot": {"provider": "mock"}, "usage_call_id": "usage-1"}
    created = auth_client.post_json(f"/api/v2/projects/{project['id']}/reviews", payload)
    assert created.status_code == 201
    loaded = auth_client.get(f"/api/v2/reviews/{created.json()['id']}")
    assert loaded.status_code == 200
    assert loaded.json()["findings"] == payload["findings"]
