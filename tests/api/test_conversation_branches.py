from __future__ import annotations

import asyncio
import threading
import uuid

from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork


def _crid() -> str:
    return uuid.uuid4().hex


def _create_project(auth_client) -> str:
    response = auth_client.post_json("/api/v1/projects", {"slug": f"proj-{uuid.uuid4().hex[:12]}", "title": "Branch Novel"})
    assert response.status_code == 201
    return response.json()["id"]


def _create_conversation(auth_client, project_id: str) -> dict:
    response = auth_client.post_json("/api/v1/conversations", {"project_id": project_id, "title": "chat"})
    assert response.status_code == 200
    return response.json()


def _send(auth_client, conversation: dict, content: str, client_request_id: str | None = None, **extra):
    payload = {"branch_id": conversation["branch_id"], "content": content, "client_request_id": client_request_id or _crid(), **extra}
    response = auth_client.post_json(f"/api/v2/conversations/{conversation['id']}/messages", payload)
    assert response.status_code == 200
    return response.json()


def test_edit_keeps_original_message_content(auth_client):
    project_id = _create_project(auth_client)
    conversation = _create_conversation(auth_client, project_id)
    sent = _send(auth_client, conversation, "original question")

    edited = auth_client.post_json(
        f"/api/v2/conversations/{conversation['id']}/messages/{sent['user_message_id']}/edit",
        {"content": "edited question"},
    )
    assert edited.status_code == 200

    main_messages = auth_client.get(f"/api/v1/conversations/{conversation['id']}/branches/{conversation['branch_id']}/messages").json()
    original = next(message for message in main_messages if message["id"] == sent["user_message_id"])
    assert original["content"] == "original question"  # 原文不可变

    tree = auth_client.get(f"/api/v2/conversations/{conversation['id']}/branches/{edited.json()['branch_id']}/tree").json()
    assert tree[-1]["content"] == "edited question"
    assert tree[-1]["id"] == edited.json()["replacement_message_id"]


def test_edit_creates_branch_with_single_parent_edge(auth_client):
    project_id = _create_project(auth_client)
    conversation = _create_conversation(auth_client, project_id)
    sent = _send(auth_client, conversation, "fork me")

    edited = auth_client.post_json(
        f"/api/v2/conversations/{conversation['id']}/messages/{sent['user_message_id']}/edit",
        {"content": "forked"},
    )
    assert edited.status_code == 200

    branches = auth_client.get(f"/api/v2/conversations/{conversation['id']}/branches").json()
    children = [branch for branch in branches if branch["parent_branch_id"] == conversation["branch_id"]]
    assert len(children) == 1  # 新分支恰一条 parent 边
    assert children[0]["id"] == edited.json()["branch_id"]
    assert children[0]["forked_from_message_id"] == sent["user_message_id"]


def test_cross_user_access_returns_404(auth_client, client, api_settings):
    project_id = _create_project(auth_client)
    conversation = _create_conversation(auth_client, project_id)
    sent = _send(auth_client, conversation, "private")

    app = client.app
    other_email = f"other-{uuid.uuid4().hex[:8]}@example.local"

    async def create_other_user():
        # 独立引擎：避免在测试线程的事件循环里复用 app loop 的连接池。
        from proseforge.infrastructure.database.session import (
            create_engine_and_sessionmaker,
        )

        engine, factory = create_engine_and_sessionmaker(api_settings)
        try:
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.users.create(other_email, app.state.auth.hash_password("twelve-char-pw"), "USER")
                await uow.commit()
        finally:
            await engine.dispose()

    asyncio.run(create_other_user())
    # 不能再开第二个裸 TestClient（每请求新 portal → asyncpg 跨 loop）；
    # 用共享 client + Bearer 头切换身份（current_user 中 Bearer 优先于 cookie）。
    login = auth_client.raw.post("/api/v1/auth/login", json={"email": other_email, "password": "twelve-char-pw"}, headers={"Origin": "http://testserver"})
    assert login.status_code == 200
    other_headers = {"Authorization": f"Bearer {login.json()['access_token']}", "Origin": "http://testserver"}

    assert auth_client.get(f"/api/v1/conversations/{conversation['id']}/branches/{conversation['branch_id']}/messages", headers=other_headers).status_code == 404
    edit = auth_client.raw.post(
        f"/api/v2/conversations/{conversation['id']}/messages/{sent['user_message_id']}/edit",
        json={"content": "hijack"},
        headers=other_headers,
    )
    assert edit.status_code == 404


def test_duplicate_client_request_id_is_idempotent(auth_client):
    project_id = _create_project(auth_client)
    conversation = _create_conversation(auth_client, project_id)
    crid = _crid()

    first = _send(auth_client, conversation, "same request", crid)
    second = _send(auth_client, conversation, "same request", crid)

    assert second["user_message_id"] == first["user_message_id"]
    assert second["assistant_message_id"] == first["assistant_message_id"]
    assert second["task_id"] == "deduplicated"
    messages = auth_client.get(f"/api/v1/conversations/{conversation['id']}/branches/{conversation['branch_id']}/messages").json()
    assert len([message for message in messages if message["role"] == "user"]) == 1


def test_reconnect_with_last_event_id_does_not_duplicate_deltas(auth_client, api_settings):
    project_id = _create_project(auth_client)
    conversation = _create_conversation(auth_client, project_id)

    async def publish_events():
        # 独立引擎：避免在测试线程的事件循环里复用 app loop 的连接池。
        from proseforge.infrastructure.database.session import (
            create_engine_and_sessionmaker,
        )
        from proseforge.infrastructure.events.database import DatabaseEventStream

        engine, factory = create_engine_and_sessionmaker(api_settings)
        try:
            stream = DatabaseEventStream(factory)
            await stream.publish(f"conversation:{conversation['id']}", {"event": "content.delta", "message_id": "m", "text": "one"})
            await stream.publish(f"conversation:{conversation['id']}", {"event": "content.delta", "message_id": "m", "text": "two"})
            await stream.publish(f"conversation:{conversation['id']}", {"event": "message.completed", "message_id": "m", "status": "COMPLETED"})
        finally:
            await engine.dispose()

    asyncio.run(publish_events())

    with auth_client.stream("GET", f"/api/v1/conversations/{conversation['id']}/events", headers={"Last-Event-ID": "1"}) as response:
        assert response.status_code == 200
        body = b"".join(response.iter_bytes()).decode()

    assert '"text": "one"' not in body  # 重放起点之后，不重复 delta
    assert body.count("event: content.delta") == 1  # 帧头计数（data 载荷里也含事件名）
    assert "two" in body
    assert "message.completed" in body  # live tail 在 terminal 事件后结束


def test_sse_stream_emits_heartbeat_comments(auth_client, api_settings, monkeypatch):
    project_id = _create_project(auth_client)
    conversation = _create_conversation(auth_client, project_id)
    monkeypatch.setattr("proseforge.api.routes.conversations.SSE_HEARTBEAT_SECONDS", 0.2)

    async def publish_terminal():
        # 独立引擎：延迟发布 terminal 事件结束 live tail，让心跳先跑两轮。
        await asyncio.sleep(0.6)
        from proseforge.infrastructure.database.session import (
            create_engine_and_sessionmaker,
        )
        from proseforge.infrastructure.events.database import DatabaseEventStream

        engine, factory = create_engine_and_sessionmaker(api_settings)
        try:
            stream = DatabaseEventStream(factory)
            await stream.publish(f"conversation:{conversation['id']}", {"event": "message.completed", "message_id": "m", "status": "COMPLETED"})
        finally:
            await engine.dispose()

    # 永不结束的流在 TestClient 中间件链下无法交付首字节（BaseHTTPMiddleware
    # call_next 等待内层首条消息），用后台线程发 terminal 让响应完整结束。
    threading.Thread(target=lambda: asyncio.run(publish_terminal()), daemon=True).start()
    with auth_client.stream("GET", f"/api/v1/conversations/{conversation['id']}/events") as response:
        assert response.status_code == 200
        body = b"".join(response.iter_bytes()).decode()

    assert ": heartbeat" in body
    assert "message.completed" in body


def test_regenerate_keeps_both_candidates(auth_client):
    project_id = _create_project(auth_client)
    conversation = _create_conversation(auth_client, project_id)
    sent = _send(auth_client, conversation, "give me two takes")

    regenerated = auth_client.post_json(
        f"/api/v2/conversations/{conversation['id']}/messages/{sent['assistant_message_id']}/regenerate",
        {"provider": "openai", "model": "gpt-4.1-mini"},
    )
    assert regenerated.status_code == 200
    candidate = regenerated.json()["message_id"]
    assert candidate != sent["assistant_message_id"]

    tree = auth_client.get(f"/api/v2/conversations/{conversation['id']}/branches/{conversation['branch_id']}/tree").json()
    assistants = [message for message in tree if message["role"] == "assistant"]
    assert len(assistants) == 2  # 两候选都在
    assert {message["id"] for message in assistants} == {sent["assistant_message_id"], candidate}


def test_v1_message_list_carries_regenerate_grouping_metadata(auth_client):
    """M9：v1 消息列表补 generation_attempt/parent_message_id，前端才能分组渲染候选。"""
    project_id = _create_project(auth_client)
    conversation = _create_conversation(auth_client, project_id)
    sent = _send(auth_client, conversation, "give me two takes")

    regenerated = auth_client.post_json(
        f"/api/v2/conversations/{conversation['id']}/messages/{sent['assistant_message_id']}/regenerate",
        {"provider": "openai", "model": "gpt-4.1-mini"},
    )
    assert regenerated.status_code == 200
    candidate = regenerated.json()["message_id"]

    messages = auth_client.get(f"/api/v1/conversations/{conversation['id']}/branches/{conversation['branch_id']}/messages").json()
    assistants = {message["id"]: message for message in messages if message["role"] == "assistant"}
    assert set(assistants) == {sent["assistant_message_id"], candidate}
    original, retry = assistants[sent["assistant_message_id"]], assistants[candidate]
    assert original["generation_attempt"] == 1 and retry["generation_attempt"] == 2
    # 两候选共享同一条父边（用户消息），前端据此归为一组。
    assert original["parent_message_id"] == sent["user_message_id"]
    assert retry["parent_message_id"] == sent["user_message_id"]


def test_v2_send_rejects_unsupported_reasoning_level(auth_client):
    project_id = _create_project(auth_client)
    conversation = _create_conversation(auth_client, project_id)
    payload = {
        "branch_id": conversation["branch_id"],
        "content": "think hard",
        "client_request_id": _crid(),
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "reasoning_level": "high",
    }
    response = auth_client.post_json(f"/api/v2/conversations/{conversation['id']}/messages", payload)
    assert response.status_code == 422
    body = response.json()
    assert "auto" in str(body)  # 响应必须列出支持级别
    assert "high" not in str(body.get("detail", {}).get("details", {}).get("supported_levels", []))


def test_v2_send_rejects_unknown_reasoning_level(auth_client):
    project_id = _create_project(auth_client)
    conversation = _create_conversation(auth_client, project_id)
    payload = {
        "branch_id": conversation["branch_id"],
        "content": "think weird",
        "client_request_id": _crid(),
        "reasoning_level": "ludicrous",
    }
    response = auth_client.post_json(f"/api/v2/conversations/{conversation['id']}/messages", payload)
    assert response.status_code == 422
    assert "supported_levels" in str(response.json())


def test_tree_entries_carry_attempt_and_parent_and_regenerate_increments(auth_client):
    project_id = _create_project(auth_client)
    conversation = _create_conversation(auth_client, project_id)
    sent = _send(auth_client, conversation, "two takes please")

    first = auth_client.post_json(
        f"/api/v2/conversations/{conversation['id']}/messages/{sent['assistant_message_id']}/regenerate",
        {"provider": "openai", "model": "gpt-4.1-mini"},
    )
    assert first.status_code == 200
    second = auth_client.post_json(
        f"/api/v2/conversations/{conversation['id']}/messages/{sent['assistant_message_id']}/regenerate",
        {"provider": "openai", "model": "gpt-4.1-mini"},
    )
    assert second.status_code == 200

    tree = auth_client.get(f"/api/v2/conversations/{conversation['id']}/branches/{conversation['branch_id']}/tree").json()
    assistants = [message for message in tree if message["role"] == "assistant"]
    assert len(assistants) == 3  # 同分支三个候选，旧候选保留
    attempts = {message["id"]: message["generation_attempt"] for message in assistants}
    assert attempts[sent["assistant_message_id"]] == 1
    assert attempts[first.json()["message_id"]] == 2
    assert attempts[second.json()["message_id"]] == 3
    # 全部候选共享同一条 parent 边（用户消息），供前端候选切换器分组
    assert {message["parent_message_id"] for message in assistants} == {sent["user_message_id"]}
    user_entry = next(message for message in tree if message["id"] == sent["user_message_id"])
    assert "generation_attempt" in user_entry and "parent_message_id" in user_entry


def test_compare_returns_common_prefix_and_message_level_diffs(auth_client):
    project_id = _create_project(auth_client)
    conversation = _create_conversation(auth_client, project_id)
    sent = _send(auth_client, conversation, "base question")

    edited = auth_client.post_json(
        f"/api/v2/conversations/{conversation['id']}/messages/{sent['user_message_id']}/edit",
        {"content": "edited question"},
    )
    assert edited.status_code == 200

    response = auth_client.get(
        f"/api/v2/conversations/{conversation['id']}/branches/compare?left={conversation['branch_id']}&right={edited.json()['branch_id']}",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["common_count"] == 1  # fork 点（原用户消息）是共同前缀
    assert [entry["id"] for entry in body["left"]] == [sent["assistant_message_id"]]
    right_tail = body["right"]
    assert [entry["id"] for entry in right_tail] == [edited.json()["replacement_message_id"]]
    assert right_tail[0]["content"] == "edited question"
    assert right_tail[0]["role"] == "user"
    assert right_tail[0]["parent_message_id"] == sent["user_message_id"]
    assert right_tail[0]["generation_attempt"] == 1


def test_archived_branch_hidden_by_default_and_visible_with_include_archived(auth_client):
    project_id = _create_project(auth_client)
    conversation = _create_conversation(auth_client, project_id)
    sent = _send(auth_client, conversation, "fork me")
    edited = auth_client.post_json(
        f"/api/v2/conversations/{conversation['id']}/messages/{sent['user_message_id']}/edit",
        {"content": "forked"},
    )
    assert edited.status_code == 200
    forked_id = edited.json()["branch_id"]

    archived = auth_client.post(f"/api/v2/conversations/{conversation['id']}/branches/{forked_id}/archive")
    assert archived.status_code == 200
    assert archived.json()["status"] == "ARCHIVED"

    default_list = auth_client.get(f"/api/v2/conversations/{conversation['id']}/branches").json()
    assert {branch["id"] for branch in default_list} == {conversation["branch_id"]}

    full_list = auth_client.get(f"/api/v2/conversations/{conversation['id']}/branches?include_archived=true").json()
    assert {branch["id"] for branch in full_list} == {conversation["branch_id"], forked_id}
    archived_entry = next(branch for branch in full_list if branch["id"] == forked_id)
    assert archived_entry["status"] == "ARCHIVED"

    # 归档是状态翻转不是删除：owner 仍能读归档分支的树
    tree = auth_client.get(f"/api/v2/conversations/{conversation['id']}/branches/{forked_id}/tree")
    assert tree.status_code == 200
    assert tree.json()[-1]["content"] == "forked"


def test_cross_user_branch_tree_compare_and_archive_return_404(auth_client, client, api_settings):
    project_id = _create_project(auth_client)
    conversation = _create_conversation(auth_client, project_id)
    _send(auth_client, conversation, "private")

    app = client.app
    other_email = f"other-{uuid.uuid4().hex[:8]}@example.local"

    async def create_other_user():
        # 独立引擎：避免在测试线程的事件循环里复用 app loop 的连接池。
        from proseforge.infrastructure.database.session import (
            create_engine_and_sessionmaker,
        )

        engine, factory = create_engine_and_sessionmaker(api_settings)
        try:
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.users.create(other_email, app.state.auth.hash_password("twelve-char-pw"), "USER")
                await uow.commit()
        finally:
            await engine.dispose()

    asyncio.run(create_other_user())
    # 同 test_cross_user_access_returns_404：共享 client + Bearer 头，不开裸 TestClient。
    login = auth_client.raw.post("/api/v1/auth/login", json={"email": other_email, "password": "twelve-char-pw"}, headers={"Origin": "http://testserver"})
    assert login.status_code == 200
    other_headers = {"Authorization": f"Bearer {login.json()['access_token']}", "Origin": "http://testserver"}

    base = f"/api/v2/conversations/{conversation['id']}/branches"
    assert auth_client.get(f"{base}/{conversation['branch_id']}/tree", headers=other_headers).status_code == 404
    assert auth_client.get(f"{base}/compare?left={conversation['branch_id']}&right={conversation['branch_id']}", headers=other_headers).status_code == 404
    archive = auth_client.raw.post(f"{base}/{conversation['branch_id']}/archive", headers=other_headers)
    assert archive.status_code == 404
    assert auth_client.get(base, headers=other_headers).json() == []  # 列表不泄露他人分支


def test_v1_send_rejects_unknown_reasoning_level(auth_client):
    """M11: v1 validates reasoning_level up front (the worker used to swallow
    unsupported levels silently). Explicit provider/model skips the
    no-available-models guard so the reasoning check is what fires."""
    project_id = _create_project(auth_client)
    conversation = _create_conversation(auth_client, project_id)
    payload = {
        "branch_id": conversation["branch_id"],
        "content": "v1 validates reasoning level",
        "client_request_id": _crid(),
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "reasoning_level": "deep",
    }
    response = auth_client.post_json(f"/api/v1/conversations/{conversation['id']}/messages", payload)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "UNSUPPORTED_REASONING_LEVEL"
