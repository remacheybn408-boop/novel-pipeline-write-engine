from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from proseforge.application.auth.service import AuthUser
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

_bearer = HTTPBearer(auto_error=False)


async def _resolve_user(request: Request, credentials: HTTPAuthorizationCredentials | None) -> AuthUser:
    """Shared token resolution for current_user / optional_user.

    Raises ValueError on any failure (missing token, bad JWT, revoked or
    unknown session); callers decide the HTTP mapping.
    """
    token = credentials.credentials if credentials else request.cookies.get("proseforge_session")
    if not token:
        raise ValueError("missing token")
    user = request.app.state.auth.decode_token(token)
    # JWTs are self-contained, but a user-level session version lets
    # password changes revoke every outstanding token immediately. Logout
    # intentionally does not bump it (see routes/auth.py logout).
    async with request.app.state.session_factory() as session:
        from proseforge.infrastructure.database.models.auth import UserModel

        record = await session.get(UserModel, user.id)
        # Read the version inside the session scope; the instance is
        # detached once the session closes.
        stored_version = None if record is None else int(record.session_version or 1)
    if stored_version is None or stored_version != user.session_version:
        raise ValueError("session has been revoked")
    return user


async def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthUser:
    try:
        return await _resolve_user(request, credentials)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid session token") from exc


async def optional_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthUser | None:
    """current_user variant for endpoints that also serve anonymous callers:
    invalid/missing credentials yield None, never a 401, and no database
    access happens without a token."""
    token = credentials.credentials if credentials else request.cookies.get("proseforge_session")
    if not token:
        return None
    try:
        return await _resolve_user(request, credentials)
    except Exception:
        return None


def require_admin(user: AuthUser) -> AuthUser:
    if user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="administrator access required")
    return user


async def require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    expected = urlsplit(request.app.state.settings.public_url)
    actual = urlsplit(origin)
    if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
        raise HTTPException(status_code=403, detail="origin is not allowed")


def unit_of_work(request: Request) -> SqlAlchemyUnitOfWork:
    return SqlAlchemyUnitOfWork(request.app.state.session_factory)


async def require_work_project(uow: SqlAlchemyUnitOfWork, user_id: str, project_id: str) -> Project:
    """Load an owned project and assert it is a work-mode project.

    Chat-mode projects are rejected with the same 404 as a missing or
    foreign project so work-only endpoints never leak existence across
    modes. Call inside an active ``async with uow:`` block.
    """
    project = await uow.projects.get_by_id(user_id, project_id)
    if project is None or project.mode != "work":
        raise HTTPException(status_code=404, detail="project not found")
    return project


async def require_owned_project(uow: SqlAlchemyUnitOfWork, user_id: str, project_id: str) -> Project:
    """Load an owned project regardless of mode (chat or work).

    Same 404 as require_work_project for missing/foreign projects; use this
    for endpoints both modes share (e.g. chat attachment uploads). Call
    inside an active ``async with uow:`` block.
    """
    project = await uow.projects.get_by_id(user_id, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project
