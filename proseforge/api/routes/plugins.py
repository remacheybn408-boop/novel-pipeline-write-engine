from __future__ import annotations

import base64
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field

from proseforge.api.dependencies import current_user, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.conversations.search_rounds import WEB_SEARCH_SKILL_KEY
from proseforge.application.conversations.tool_contract import (
    CODE_RUNNER_SKILL_KEY,
    DOC_READER_SKILL_KEY,
    WEB_READER_SKILL_KEY,
)
from proseforge.application.plugins.builtin_skills import (
    BuiltinSkill,
    load_builtin_skills,
)
from proseforge.application.plugins.skill_import import (
    extract_skill_from_archive,
    parse_skill_markdown,
)
from proseforge.domain.common.ids import new_id
from proseforge.domain.plugin.entity import McpServer, Skill
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.mcp.client import probe_mcp_server
from proseforge.infrastructure.security.credential_cipher import (
    CredentialCipher,
    derive_key,
)
from proseforge.infrastructure.security.endpoint_policy import EndpointPolicy

router = APIRouter(prefix="/api/v1/plugins", tags=["plugins"])

MAX_SKILLS_PER_USER = 50
MAX_MCP_SERVERS_PER_USER = 20
MAX_UPLOAD_BYTES = 1024 * 1024  # 1MB


class SkillRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    content: str = Field(min_length=1)
    enabled: bool = True


class SkillPatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None
    content: str | None = Field(default=None, min_length=1)
    enabled: bool | None = None


class McpServerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    transport: Literal["streamable-http", "sse"]
    url: str = Field(min_length=1, max_length=2000)
    headers: dict[str, str] | None = None
    enabled: bool = True


class McpServerPatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    transport: Literal["streamable-http", "sse"] | None = None
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    headers: dict[str, str] | None = None
    enabled: bool | None = None


def _cipher_for(request: Request) -> CredentialCipher:
    raw = request.app.state.settings.master_key.get_secret_value()
    return CredentialCipher(derive_key(raw))


def _policy_for(request: Request) -> EndpointPolicy:
    return EndpointPolicy(tuple(request.app.state.settings.allowed_local_provider_hosts))


def _encrypt_headers(request: Request, user_id: str, record_id: str, headers: dict[str, str]) -> str:
    associated = f"{user_id}:mcp:{record_id}".encode()
    encrypted = _cipher_for(request).encrypt(json.dumps(headers).encode(), associated_data=associated)
    return base64.b64encode(encrypted).decode()


def _decrypt_headers(request: Request, server: McpServer) -> dict[str, str]:
    if not server.encrypted_headers:
        return {}
    associated = f"{server.user_id}:mcp:{server.id}".encode()
    try:
        raw = _cipher_for(request).decrypt(base64.b64decode(server.encrypted_headers), associated_data=associated)
        parsed = json.loads(raw)
    except Exception:
        return {}
    return {str(key): str(value) for key, value in parsed.items()} if isinstance(parsed, dict) else {}


def _skill_payload(skill: Skill) -> dict[str, object]:
    return {"id": skill.id, "name": skill.name, "description": skill.description, "content": skill.content, "enabled": skill.enabled, "created_at": skill.created_at.isoformat(), "skill_key": None, "builtin": False, "category": None}


def _builtin_payload(skill: BuiltinSkill, enabled: bool) -> dict[str, object]:
    return {"id": f"builtin:{skill.skill_key}", "skill_key": skill.skill_key, "name": skill.name, "description": skill.description, "content": skill.content, "enabled": enabled, "builtin": True, "created_at": None, "category": skill.category}


async def _builtin_enabled_map(uow: SqlAlchemyUnitOfWork, user_id: str) -> dict[str, bool]:
    # 无状态行 → 默认 disabled（避免 11 个内置 skill 全注入撑爆上下文）
    return {state.skill_key: state.enabled for state in await uow.builtin_skill_states.list_for_user(user_id)}


def _mcp_payload(request: Request, server: McpServer) -> dict[str, object]:
    return {
        "id": server.id, "name": server.name, "transport": server.transport, "url": server.url,
        "enabled": server.enabled, "created_at": server.created_at.isoformat(),
        "header_keys": sorted(_decrypt_headers(request, server).keys()),  # 绝不返回 header 值
    }


# --- Skills ---


@router.get("/skills")
async def list_skills(
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> list[dict[str, object]]:
    async with uow:
        from proseforge.application.work.retriever import NARRATIVE_RAG_SKILL_KEY

        enabled_map = await _builtin_enabled_map(uow, user.id)
        builtin = [
            # narrative-rag defaults ON (no state row = enabled); every
            # other builtin skill defaults OFF.
            _builtin_payload(skill, enabled_map.get(skill.skill_key, skill.skill_key == NARRATIVE_RAG_SKILL_KEY))
            for skill in load_builtin_skills(request.app.state.settings.skills_dir)
        ]
        return [*builtin, *[_skill_payload(skill) for skill in await uow.skills.list_for_user(user.id)]]


class BuiltinSkillPatchRequest(BaseModel):
    enabled: bool


# 注意：必须注册在 PATCH /skills/{skill_id} 之前，否则 "builtin" 会被 {skill_id} 捕获。
@router.patch("/skills/builtin/{skill_key}")
async def update_builtin_skill(
    skill_key: str,
    payload: BuiltinSkillPatchRequest,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    skill = next((item for item in load_builtin_skills(request.app.state.settings.skills_dir) if item.skill_key == skill_key), None)
    if skill is None:
        raise HTTPException(status_code=404, detail="builtin skill not found")
    async with uow:
        await uow.builtin_skill_states.upsert(user.id, skill_key, payload.enabled)
        await uow.commit()
        return _builtin_payload(skill, payload.enabled)


# --- Builtin tool switches ---


@router.get("/tools/web-search")
async def get_web_search_switch(
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, bool]:
    # No state row = disabled (same default as builtin skills).
    async with uow:
        enabled = any(state.skill_key == WEB_SEARCH_SKILL_KEY and state.enabled for state in await uow.builtin_skill_states.list_for_user(user.id))
        return {"enabled": enabled}


@router.patch("/tools/web-search")
async def set_web_search_switch(
    payload: BuiltinSkillPatchRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, bool]:
    async with uow:
        await uow.builtin_skill_states.upsert(user.id, WEB_SEARCH_SKILL_KEY, payload.enabled)
        await uow.commit()
        return {"enabled": payload.enabled}


async def _get_tool_switch(uow: SqlAlchemyUnitOfWork, user_id: str, skill_key: str) -> dict[str, bool]:
    # No state row = disabled (same default as builtin skills).
    async with uow:
        enabled = any(state.skill_key == skill_key and state.enabled for state in await uow.builtin_skill_states.list_for_user(user_id))
        return {"enabled": enabled}


async def _set_tool_switch(uow: SqlAlchemyUnitOfWork, user_id: str, skill_key: str, enabled: bool) -> dict[str, bool]:
    async with uow:
        await uow.builtin_skill_states.upsert(user_id, skill_key, enabled)
        await uow.commit()
        return {"enabled": enabled}


@router.get("/tools/web-reader")
async def get_web_reader_switch(
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, bool]:
    return await _get_tool_switch(uow, user.id, WEB_READER_SKILL_KEY)


@router.patch("/tools/web-reader")
async def set_web_reader_switch(
    payload: BuiltinSkillPatchRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, bool]:
    return await _set_tool_switch(uow, user.id, WEB_READER_SKILL_KEY, payload.enabled)


@router.get("/tools/doc-reader")
async def get_doc_reader_switch(
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, bool]:
    return await _get_tool_switch(uow, user.id, DOC_READER_SKILL_KEY)


@router.patch("/tools/doc-reader")
async def set_doc_reader_switch(
    payload: BuiltinSkillPatchRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, bool]:
    return await _set_tool_switch(uow, user.id, DOC_READER_SKILL_KEY, payload.enabled)


@router.get("/tools/code-runner")
async def get_code_runner_switch(
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, bool]:
    return await _get_tool_switch(uow, user.id, CODE_RUNNER_SKILL_KEY)


@router.patch("/tools/code-runner")
async def set_code_runner_switch(
    payload: BuiltinSkillPatchRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, bool]:
    return await _set_tool_switch(uow, user.id, CODE_RUNNER_SKILL_KEY, payload.enabled)


@router.post("/skills", status_code=status.HTTP_201_CREATED)
async def create_skill(
    payload: SkillRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        if await uow.skills.count_for_user(user.id) >= MAX_SKILLS_PER_USER:
            raise HTTPException(status_code=409, detail="skill limit reached")
        if await uow.skills.get_by_name(user.id, payload.name) is not None:
            raise HTTPException(status_code=409, detail="skill name already exists")
        skill = Skill.create(user_id=user.id, name=payload.name, description=payload.description, content=payload.content, enabled=payload.enabled)
        await uow.skills.add(skill)
        await uow.commit()
        return _skill_payload(skill)


@router.post("/skills/upload", status_code=status.HTTP_201_CREATED)
async def upload_skill(
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
    file: Annotated[UploadFile, File()],
) -> dict[str, object]:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=422, detail="skill file exceeds 1MB limit")
    try:
        inner_name, text = extract_skill_from_archive(file.filename or "skill.md", data)
        parsed = parse_skill_markdown(inner_name, text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"skill file could not be parsed: {exc}") from exc
    if len(parsed.name) > 64:
        raise HTTPException(status_code=422, detail="skill name exceeds 64 characters")
    async with uow:
        if await uow.skills.count_for_user(user.id) >= MAX_SKILLS_PER_USER:
            raise HTTPException(status_code=409, detail="skill limit reached")
        if await uow.skills.get_by_name(user.id, parsed.name) is not None:
            raise HTTPException(status_code=409, detail="skill name already exists")
        skill = Skill.create(user_id=user.id, name=parsed.name, description=parsed.description, content=parsed.content)
        await uow.skills.add(skill)
        await uow.commit()
        return _skill_payload(skill)


@router.patch("/skills/{skill_id}")
async def update_skill(
    skill_id: str,
    payload: SkillPatchRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    # 只管用户自建 skill（uuid id）；内置项走 PATCH /skills/builtin/{skill_key}，
    # "builtin:" 前缀的 id 在这里自然查不到（404），无需额外防御。
    async with uow:
        if payload.name is not None:
            existing = await uow.skills.get_by_name(user.id, payload.name)
            if existing is not None and existing.id != skill_id:
                raise HTTPException(status_code=409, detail="skill name already exists")
        skill = await uow.skills.update(user.id, skill_id, **payload.model_dump(exclude_unset=True))
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        await uow.commit()
        return _skill_payload(skill)


@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    skill_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> None:
    async with uow:
        if not await uow.skills.delete(user.id, skill_id):
            raise HTTPException(status_code=404, detail="skill not found")
        await uow.commit()


# --- MCP servers ---


@router.get("/mcp-servers")
async def list_mcp_servers(
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> list[dict[str, object]]:
    async with uow:
        return [_mcp_payload(request, server) for server in await uow.mcp_servers.list_for_user(user.id)]


@router.post("/mcp-servers", status_code=status.HTTP_201_CREATED)
async def create_mcp_server(
    payload: McpServerRequest,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    try:
        _policy_for(request).validate(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    async with uow:
        if await uow.mcp_servers.count_for_user(user.id) >= MAX_MCP_SERVERS_PER_USER:
            raise HTTPException(status_code=409, detail="mcp server limit reached")
        if await uow.mcp_servers.get_by_name(user.id, payload.name) is not None:
            raise HTTPException(status_code=409, detail="mcp server name already exists")
        record_id = new_id()
        encrypted_headers = _encrypt_headers(request, user.id, record_id, payload.headers) if payload.headers else None
        server = McpServer.create(user_id=user.id, name=payload.name, transport=payload.transport, url=payload.url, encrypted_headers=encrypted_headers, enabled=payload.enabled, record_id=record_id)
        await uow.mcp_servers.add(server)
        await uow.commit()
        return _mcp_payload(request, server)


@router.patch("/mcp-servers/{server_id}")
async def update_mcp_server(
    server_id: str,
    payload: McpServerPatchRequest,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    if payload.url is not None:
        try:
            _policy_for(request).validate(payload.url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    async with uow:
        if payload.name is not None:
            existing = await uow.mcp_servers.get_by_name(user.id, payload.name)
            if existing is not None and existing.id != server_id:
                raise HTTPException(status_code=409, detail="mcp server name already exists")
        updates = payload.model_dump(exclude_unset=True)
        raw_headers = updates.pop("headers", None)
        if raw_headers is not None:
            # headers 传了才重加密（AD 绑定 record_id）；不传保留原密文。
            # 显式传 {} 表示清空，同样落密文（解密后为零键）。
            updates["encrypted_headers"] = _encrypt_headers(request, user.id, server_id, raw_headers)
        server = await uow.mcp_servers.update(user.id, server_id, **updates)
        if server is None:
            raise HTTPException(status_code=404, detail="mcp server not found")
        await uow.commit()
        return _mcp_payload(request, server)


@router.delete("/mcp-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(
    server_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> None:
    async with uow:
        if not await uow.mcp_servers.delete(user.id, server_id):
            raise HTTPException(status_code=404, detail="mcp server not found")
        await uow.commit()


@router.post("/mcp-servers/{server_id}/probe")
async def probe_server(
    server_id: str,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        server = await uow.mcp_servers.get_for_user(user.id, server_id)
        if server is None:
            raise HTTPException(status_code=404, detail="mcp server not found")
        headers = _decrypt_headers(request, server)
    try:
        url = _policy_for(request).validate(server.url)  # SSRF 探活前再校验一次
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return await probe_mcp_server(url, server.transport, headers)
