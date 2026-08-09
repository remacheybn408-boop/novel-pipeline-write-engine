from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ORIGIN = "http://testserver"  # 与 tests/api/conftest.py 一致（require_same_origin 期望）

# 与 tests/api/test_agent_security.py 的分工：安全语义（签名快照、篡改 fail-closed、
# 并发额度、幂等回放、429 封套）由该文件覆盖；本文件锁 V3 端点表面的正常路径与
# 校验语义（控制迁移、事件游标、Artifact/Review、chief-proposal、expand/accept、
# metrics/memories、跨用户 404）。


def _database_url() -> str:
    url = os.environ.get("PROSEFORGE_TEST_DATABASE_URL")
    assert url, "PROSEFORGE_TEST_DATABASE_URL is required (API tests run in the B1 batch)"
    return url


def _run_sql(statement: str, **params):
    """一次性引擎直改/直读数据库（独立事件循环，避免与 TestClient portal 争用连接）。"""

    async def _execute():
        engine = create_async_engine(_database_url())
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement), params)
                return result.mappings().all() if result.returns_rows else None
        finally:
            await engine.dispose()

    return asyncio.run(_execute())


@pytest.fixture()
def v3_user(client, api_settings, user_headers_factory):
    """隔离用户的 bearer 会话：/api/v1/auth/setup 是一次性 owner 端点，无法注册
    第二账号；current_user 会用 token 的 session_version 查 users 表，因此必须
    真实落库一行用户记录。V3 端点按 user.id 隔离数据，限流桶与并发额度也按
    user.id 划分——每个用例独立用户，套件内互不挤桶。"""
    user_id = f"v3-endpoints-{uuid.uuid4().hex[:12]}"
    headers = user_headers_factory(user_id)
    return client, headers, user_id


def _create_project(api, headers: dict) -> dict:
    response = api.post("/api/v1/projects", json={"slug": f"v3e-{uuid.uuid4().hex[:12]}", "title": "V3 Endpoints"}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _create_run(api, headers: dict, project_id: str, payload: dict | None = None) -> dict:
    response = api.post(f"/api/v3/projects/{project_id}/agent-runs", json={"goal": "endpoint surface", **(payload or {})}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _create_artifact(api, headers: dict, run_id: str, payload: dict | None = None) -> dict:
    body = {"artifact_type": "candidate", "payload": {"scene": "candidate text"}, "provenance": {"source": "test"}, "preview": "pv", **(payload or {})}
    response = api.post(f"/api/v3/agent-runs/{run_id}/artifacts", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_pause_resume_cancel_transitions(v3_user):
    api, headers, _ = v3_user
    run = _create_run(api, headers, _create_project(api, headers)["id"])
    assert run["status"] == "PENDING"
    assert run["policy_version"] == "v3-policy-1"
    assert run["event_cursor"] >= 1

    paused = api.post(f"/api/v3/agent-runs/{run['id']}/pause", headers=headers)
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "PAUSED"
    # 同目标状态重放幂等（200，不重复迁移）
    assert api.post(f"/api/v3/agent-runs/{run['id']}/pause", headers=headers).json()["status"] == "PAUSED"
    assert api.get(f"/api/v3/agent-runs/{run['id']}", headers=headers).json()["status"] == "PAUSED"

    resumed = api.post(f"/api/v3/agent-runs/{run['id']}/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "RUNNING"

    cancelled = api.post(f"/api/v3/agent-runs/{run['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["terminal_reason"] == "cancelled by user"
    # 终态后的非法迁移一律 409 INVALID_RUN_TRANSITION
    for action in ("pause", "resume"):
        rejected = api.post(f"/api/v3/agent-runs/{run['id']}/{action}", headers=headers)
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["error"]["code"] == "INVALID_RUN_TRANSITION"


def test_events_after_cursor_replay(v3_user):
    api, headers, _ = v3_user
    run = _create_run(api, headers, _create_project(api, headers)["id"])
    # 追加一个控制事件，让 after 过滤有可观察的分界（created=1, pause=2）。
    assert api.post(f"/api/v3/agent-runs/{run['id']}/pause", headers=headers).status_code == 200

    page = api.get(f"/api/v3/agent-runs/{run['id']}/events?after=0", headers=headers).json()
    assert [event["event"] for event in page["events"]] == ["run.created", "run.pause"]
    assert page["events"][0]["sequence"] == 1
    assert page["next_cursor"] == page["events"][-1]["sequence"]

    tail = api.get(f"/api/v3/agent-runs/{run['id']}/events?after=1", headers=headers).json()
    assert [event["event"] for event in tail["events"]] == ["run.pause"]

    replay = api.get(f"/api/v3/agent-runs/{run['id']}/events?after={page['next_cursor']}", headers=headers).json()
    assert replay["events"] == []
    assert replay["next_cursor"] == page["next_cursor"]


def test_artifacts_create_and_list(v3_user):
    api, headers, _ = v3_user
    run = _create_run(api, headers, _create_project(api, headers)["id"])
    artifact = _create_artifact(api, headers, run["id"])
    assert artifact["artifact_type"] == "candidate"
    expected_sha = hashlib.sha256(json.dumps({"scene": "candidate text"}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assert artifact["sha256"] == expected_sha

    listed = api.get(f"/api/v3/agent-runs/{run['id']}/artifacts", headers=headers)
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["id"] == artifact["id"]
    assert rows[0]["sha256"] == artifact["sha256"]
    assert rows[0]["preview"] == "pv"
    assert rows[0]["provenance"] == {"source": "test"}

    events = api.get(f"/api/v3/agent-runs/{run['id']}/events", headers=headers).json()["events"]
    assert any(event["event"] == "artifact.committed" and event["data"]["artifact_id"] == artifact["id"] for event in events)


def test_reviews_create_list_and_validation(v3_user):
    api, headers, _ = v3_user
    run = _create_run(api, headers, _create_project(api, headers)["id"])
    artifact = _create_artifact(api, headers, run["id"])

    created = api.post(
        f"/api/v3/agent-runs/{run['id']}/reviews",
        json={"artifact_id": artifact["id"], "reviewer_role": "continuity_reviewer", "status": "CONFLICT", "conflict_group": "g1", "evidence": [{"rule": "continuity", "result": "needs-chief-editor"}]},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "CONFLICT"
    assert body["conflict_group"] == "g1"
    assert body["evidence"] == [{"rule": "continuity", "result": "needs-chief-editor"}]

    listed = api.get(f"/api/v3/agent-runs/{run['id']}/reviews", headers=headers).json()
    assert len(listed) == 1
    assert listed[0]["id"] == body["id"]
    assert listed[0]["reviewer_role"] == "continuity_reviewer"

    # 非 UNSUPPORTED 评审必须带证据
    missing_evidence = api.post(
        f"/api/v3/agent-runs/{run['id']}/reviews",
        json={"artifact_id": artifact["id"], "reviewer_role": "style_editor", "status": "WARNING"},
        headers=headers,
    )
    assert missing_evidence.status_code == 422
    # UNSUPPORTED 允许空证据
    unsupported = api.post(
        f"/api/v3/agent-runs/{run['id']}/reviews",
        json={"artifact_id": artifact["id"], "reviewer_role": "style_editor", "status": "UNSUPPORTED"},
        headers=headers,
    )
    assert unsupported.status_code == 201, unsupported.text
    # 评审目标必须是本 run 的 Artifact
    unknown_artifact = api.post(
        f"/api/v3/agent-runs/{run['id']}/reviews",
        json={"artifact_id": "missing", "reviewer_role": "style_editor", "status": "PASS", "evidence": [{"rule": "x"}]},
        headers=headers,
    )
    assert unknown_artifact.status_code == 404
    # 状态词表白名单之外的取值被 schema 拒绝
    bad_status = api.post(
        f"/api/v3/agent-runs/{run['id']}/reviews",
        json={"artifact_id": artifact["id"], "reviewer_role": "style_editor", "status": "BOGUS", "evidence": [{"rule": "x"}]},
        headers=headers,
    )
    assert bad_status.status_code == 422


def test_chief_proposal_requires_completed_run(v3_user):
    api, headers, _ = v3_user
    project = _create_project(api, headers)
    chapter = api.post(f"/api/v1/projects/{project['id']}/chapters", json={"chapter_no": 1, "title": "The Checkpoint"}, headers=headers)
    assert chapter.status_code == 201, chapter.text
    base = api.post(f"/api/v1/chapters/{chapter.json()['id']}/versions", json={"content": "The first checkpoint held."}, headers=headers)
    assert base.status_code == 201, base.text
    run = _create_run(api, headers, project["id"], {"chapter_id": chapter.json()["id"], "base_version_id": base.json()["id"]})

    not_completed = api.post(f"/api/v3/agent-runs/{run['id']}/chief-proposal", headers=headers)
    assert not_completed.status_code == 409, not_completed.text
    assert not_completed.json()["error"]["code"] == "RUN_NOT_COMPLETED"

    _run_sql("UPDATE agent_runs SET status = 'COMPLETED' WHERE id = :id", id=run["id"])
    created = api.post(f"/api/v3/agent-runs/{run['id']}/chief-proposal", headers=headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["proposal_id"]
    assert body["guard_status"] == "clear"

    # run.proposal_id 幂等：重复触发回放既有提案（200，不新建）
    replay = api.post(f"/api/v3/agent-runs/{run['id']}/chief-proposal", headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["proposal_id"] == body["proposal_id"]
    proposals = api.get(f"/api/v2/chapters/{chapter.json()['id']}/proposals", headers=headers).json()
    assert len(proposals) == 1
    assert proposals[0]["id"] == body["proposal_id"]
    assert proposals[0]["status"] == "PROPOSED"
    assert proposals[0]["guard_status"] == "clear"


def test_expand_happy_path_and_violations(v3_user):
    api, headers, _ = v3_user
    run = _create_run(api, headers, _create_project(api, headers)["id"])
    tasks = api.get(f"/api/v3/agent-runs/{run['id']}/tasks", headers=headers).json()
    parent = tasks[0]
    suffix = uuid.uuid4().hex[:8]
    expansion = {
        "children": [{"role": "scene_writer", "token_budget": 2}],
        "expansion_reason": f"need one more scene {suffix}",
        "dedupe_key": f"dk-{suffix}",
    }

    # 父任务非 terminal-SUCCEEDED：dry-run 与正式扩展给出一致违规
    dry_run = api.post(f"/api/v3/agent-runs/{run['id']}/graph/validate", json={**expansion, "parent_task_id": parent["id"]}, headers=headers)
    assert dry_run.status_code == 200, dry_run.text
    assert dry_run.json()["valid"] is False
    assert "parent task is not terminal-SUCCEEDED" in dry_run.json()["violations"]
    pending = api.post(f"/api/v3/agent-runs/{run['id']}/tasks/{parent['id']}/expand", json=expansion, headers=headers)
    assert pending.status_code == 422, pending.text
    assert pending.json()["error"]["code"] == "EXPANSION_INVALID"
    assert "parent task is not terminal-SUCCEEDED" in pending.json()["error"]["details"]["violations"]

    _run_sql("UPDATE agent_tasks SET status = 'SUCCEEDED' WHERE id = :id", id=parent["id"])
    expanded = api.post(f"/api/v3/agent-runs/{run['id']}/tasks/{parent['id']}/expand", json=expansion, headers=headers)
    assert expanded.status_code == 201, expanded.text
    body = expanded.json()
    assert body["graph_revision"] == 2
    assert body["dedupe_key"] == expansion["dedupe_key"]
    assert len(body["tasks"]) == 1
    child = body["tasks"][0]
    assert child["task_key"] == f"{parent['task_key']}-expand-1"
    assert child["role"] == "scene_writer"
    assert child["depends_on"] == [parent["task_key"]]
    assert len(api.get(f"/api/v3/agent-runs/{run['id']}/tasks", headers=headers).json()) == 3
    assert api.get(f"/api/v3/agent-runs/{run['id']}", headers=headers).json()["graph_revision"] == 2

    # dedupe_key / expansion_reason 每 run 唯一
    duplicate = api.post(f"/api/v3/agent-runs/{run['id']}/tasks/{parent['id']}/expand", json=expansion, headers=headers)
    assert duplicate.status_code == 422
    assert "duplicate dedupe key for this run" in duplicate.json()["error"]["details"]["violations"]
    same_reason = api.post(
        f"/api/v3/agent-runs/{run['id']}/tasks/{parent['id']}/expand",
        json={**expansion, "dedupe_key": f"dk-fresh-{suffix}"},
        headers=headers,
    )
    assert same_reason.status_code == 422
    assert "expansion reason already used in this run" in same_reason.json()["error"]["details"]["violations"]
    # 角色目录之外的子任务角色被拒
    unknown_role = api.post(
        f"/api/v3/agent-runs/{run['id']}/graph/validate",
        json={"children": [{"role": "ghost_writer"}], "expansion_reason": f"unknown role {suffix}", "dedupe_key": f"dk-role-{suffix}", "parent_task_id": parent["id"]},
        headers=headers,
    )
    assert unknown_role.json()["valid"] is False
    assert "unknown role: ghost_writer" in unknown_role.json()["violations"]
    # 合法 dry-run：新 dedupe/reason + 显式 task_key（默认派生键已被上面的正式扩展占用），父已 SUCCEEDED
    valid = api.post(
        f"/api/v3/agent-runs/{run['id']}/graph/validate",
        json={"children": [{"task_key": f"timeline-{suffix}", "role": "timeline_analyst", "token_budget": 1}], "expansion_reason": f"timeline check {suffix}", "dedupe_key": f"dk-ok-{suffix}", "parent_task_id": parent["id"]},
        headers=headers,
    )
    assert valid.json() == {"valid": True, "violations": []}


def test_accept_endpoint_flips_memory_candidate(v3_user):
    api, headers, _ = v3_user
    project = _create_project(api, headers)
    run = _create_run(api, headers, project["id"])
    artifact = _create_artifact(api, headers, run["id"])
    memory_id = f"mem-{uuid.uuid4().hex[:12]}"
    value = json.dumps({"value": "Mira carries the brass map.", "confidence": 0.9, "revision": 1}, ensure_ascii=False, sort_keys=True)
    _run_sql(
        "INSERT INTO agent_memories (id, project_id, run_id, memory_key, value, source_artifact_id, status)"
        " VALUES (:id, :project_id, :run_id, :memory_key, :value, :source_artifact_id, 'PENDING')",
        id=memory_id,
        project_id=project["id"],
        run_id=run["id"],
        memory_key="mira-map",
        value=value,
        source_artifact_id=artifact["id"],
    )

    accepted = api.post(f"/api/v3/agent-runs/{run['id']}/artifacts/{artifact['id']}/accept", json={"decision": "accept"}, headers=headers)
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["updated"] == 1
    assert body["decision"] == "accept"
    assert body["memories"][0]["id"] == memory_id
    assert body["memories"][0]["status"] == "ACCEPTED"
    assert body["memories"][0]["value"] == "Mira carries the brass map."
    assert body["memories"][0]["confidence"] == 0.9

    memories = api.get(f"/api/v3/agent-runs/{run['id']}/memories?status=ACCEPTED", headers=headers).json()
    assert [row["id"] for row in memories] == [memory_id]
    assert api.get(f"/api/v3/agent-runs/{run['id']}/memories?status=PENDING", headers=headers).json() == []
    unknown_status = api.get(f"/api/v3/agent-runs/{run['id']}/memories?status=BOGUS", headers=headers)
    assert unknown_status.status_code == 422

    # 无 PENDING 候选时 updated=0；指定未知 memory_ids 时 404
    assert api.post(f"/api/v3/agent-runs/{run['id']}/artifacts/{artifact['id']}/accept", json={"decision": "reject"}, headers=headers).json()["updated"] == 0
    missing = api.post(
        f"/api/v3/agent-runs/{run['id']}/artifacts/{artifact['id']}/accept",
        json={"decision": "accept", "memory_ids": ["missing"]},
        headers=headers,
    )
    assert missing.status_code == 404
    unknown_artifact = api.post(f"/api/v3/agent-runs/{run['id']}/artifacts/missing/accept", json={"decision": "accept"}, headers=headers)
    assert unknown_artifact.status_code == 404


def test_metrics_reports_counts_without_content(v3_user):
    api, headers, _ = v3_user
    run = _create_run(api, headers, _create_project(api, headers)["id"])
    metrics = api.get(f"/api/v3/agent-runs/{run['id']}/metrics", headers=headers)
    assert metrics.status_code == 200, metrics.text
    body = metrics.json()
    assert body["run_id"] == run["id"]
    assert body["status"] == "PENDING"
    assert body["task_counts"] == {"PENDING": 2}
    assert body["task_total"] == 2
    assert body["budget_limit"] == 12000
    assert body["budget_used"] == 0
    assert body["graph_revision"] == 1
    assert body["evaluations"] == []


def test_create_run_validates_graph_budget_and_ownership(v3_user):
    api, headers, _ = v3_user
    project = _create_project(api, headers)
    over_budget = api.post(
        f"/api/v3/projects/{project['id']}/agent-runs",
        json={"goal": "over budget", "budget_limit": 5, "tasks": [{"id": "a", "role": "chief_planner", "token_budget": 10}]},
        headers=headers,
    )
    assert over_budget.status_code == 422
    missing_chapter = api.post(
        f"/api/v3/projects/{project['id']}/agent-runs",
        json={"goal": "missing chapter", "chapter_id": "missing"},
        headers=headers,
    )
    assert missing_chapter.status_code == 404
    missing_project = api.post("/api/v3/projects/missing/agent-runs", json={"goal": "missing project"}, headers=headers)
    assert missing_project.status_code == 404


def test_cross_user_isolation_returns_404(v3_user, client, api_settings, user_headers_factory):
    api, headers, _ = v3_user
    run = _create_run(api, headers, _create_project(api, headers)["id"])
    artifact = _create_artifact(api, headers, run["id"])

    stranger_id = f"v3-stranger-{uuid.uuid4().hex[:12]}"
    stranger = user_headers_factory(stranger_id)

    for path in ("", "/tasks", "/events", "/artifacts", "/reviews", "/audit", "/metrics", "/memories"):
        response = api.get(f"/api/v3/agent-runs/{run['id']}{path}", headers=stranger)
        assert response.status_code == 404, f"GET {path or '/'} leaked across users"
    for action in ("pause", "resume", "cancel", "retry", "chief-proposal"):
        response = api.post(f"/api/v3/agent-runs/{run['id']}/{action}", headers=stranger)
        assert response.status_code == 404, f"POST {action} leaked across users"
    write_surfaces = (
        ("/artifacts", {"artifact_type": "candidate", "payload": {"x": 1}}),
        ("/reviews", {"artifact_id": artifact["id"], "reviewer_role": "style_editor", "status": "UNSUPPORTED"}),
        (f"/artifacts/{artifact['id']}/accept", {"decision": "accept"}),
    )
    for suffix, payload in write_surfaces:
        response = api.post(f"/api/v3/agent-runs/{run['id']}{suffix}", json=payload, headers=stranger)
        assert response.status_code == 404, f"POST {suffix} leaked across users"
