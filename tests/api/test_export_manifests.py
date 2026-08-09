from __future__ import annotations

import hashlib
import uuid


def _story(auth_client):
    project = auth_client.post_json(
        "/api/v1/projects",
        {"slug": f"export-{uuid.uuid4().hex[:12]}", "title": "The Glass Harbor"},
    ).json()
    chapter = auth_client.post_json(
        f"/api/v1/projects/{project['id']}/chapters",
        {"chapter_no": 1, "title": "Arrival"},
    ).json()
    version = auth_client.post_json(
        f"/api/v1/chapters/{chapter['id']}/versions",
        {"content": "Rain crossed the harbor."},
    ).json()
    return project, chapter, version


def test_export_resolves_active_version_and_persists_reproducible_manifest(auth_client):
    project, _, version = _story(auth_client)

    response = auth_client.post_json(
        f"/api/v1/projects/{project['id']}/exports",
        {"format": "md", "template": "archive", "locale": "en-US", "title": "Glass Harbor", "author": "M. Vale"},
    )

    assert response.status_code == 201
    manifest = response.json()
    assert manifest["version_ids"] == [version["id"]]
    assert manifest["content_hashes"] == {version["id"]: hashlib.sha256(b"Rain crossed the harbor.").hexdigest()}

    downloaded = auth_client.get(manifest["download_url"])
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == manifest["file_sha256"]
    assert int(manifest["byte_size"]) == len(downloaded.content)
    assert downloaded.headers["x-proseforge-manifest-id"] == manifest["id"]

    persisted = auth_client.get(f"/api/v1/projects/{project['id']}/exports/{manifest['id']}")
    assert persisted.status_code == 200
    assert persisted.json() == manifest


def test_export_presets_produce_distinct_verifiable_hashes(auth_client):
    project, _, version = _story(auth_client)
    hashes = set()
    for template in ("web-serial", "submission", "archive"):
        response = auth_client.post_json(
            f"/api/v1/projects/{project['id']}/exports",
            {"format": "txt", "template": template, "version_ids": [version["id"]], "locale": "zh-CN", "title": "玻璃港", "author": "未央"},
        )
        assert response.status_code == 201
        manifest = response.json()
        downloaded = auth_client.get(manifest["download_url"])
        assert hashlib.sha256(downloaded.content).hexdigest() == manifest["file_sha256"]
        hashes.add(manifest["file_sha256"])
    assert len(hashes) == 3


def test_export_rejects_multiple_versions_of_the_same_chapter(auth_client):
    project, chapter, first = _story(auth_client)
    second = auth_client.post_json(
        f"/api/v1/chapters/{chapter['id']}/versions",
        {"content": "A second immutable draft.", "base_version": first["version_no"]},
    ).json()
    response = auth_client.post_json(
        f"/api/v1/projects/{project['id']}/exports",
        {"format": "md", "template": "archive", "version_ids": [first["id"], second["id"]]},
    )
    assert response.status_code == 422
