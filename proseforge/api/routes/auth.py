from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError

from proseforge.api.dependencies import (
    current_user,
    require_same_origin,
    unit_of_work,
)
from proseforge.application.auth.service import AuthService, AuthUser
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# Real argon2 hash computed lazily (once per process) so that logins with an
# unknown email pay the same verification cost as known users; otherwise the
# short-circuit would leak account existence through response timing.
_DUMMY_PASSWORD_HASH: str | None = None

# Deliberately permissive: a single @, a dot in the domain, no whitespace.
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _dummy_password_hash(auth: AuthService) -> str:
    global _DUMMY_PASSWORD_HASH
    if _DUMMY_PASSWORD_HASH is None:
        _DUMMY_PASSWORD_HASH = auth.hash_password("dummy-password-000")
    return _DUMMY_PASSWORD_HASH


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12)


class SetupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12)

    @field_validator("email")
    @classmethod
    def _email_must_look_like_an_address(cls, value: str) -> str:
        # Basic shape check only (local@domain.tld); deliverability is not
        # verified anywhere in the product.
        if not _EMAIL_PATTERN.match(value):
            raise ValueError("invalid email address")
        return value


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=12)


@router.post("/setup", status_code=status.HTTP_201_CREATED)
async def setup_admin(
    payload: SetupRequest,
    http_request: Request,
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
    _csrf: Annotated[None, Depends(require_same_origin)],
) -> dict[str, str]:
    async with uow:
        if await uow.users.count() > 0:
            raise HTTPException(status_code=409, detail="initial setup already completed")
        try:
            user = await uow.users.create(payload.email, http_request.app.state.auth.hash_password(payload.password), "ADMIN")
            await uow.commit()
        except IntegrityError:
            # Only the unique-email race maps to 409; any other database
            # failure must surface as a 500 instead of a misleading conflict.
            await uow.rollback()
            raise HTTPException(status_code=409, detail="email already registered") from None
        return {"id": user.id, "email": user.email, "role": user.role}


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_user(
    payload: SetupRequest,
    http_request: Request,
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
    _csrf: Annotated[None, Depends(require_same_origin)],
) -> dict[str, str]:
    """Self-service registration for shared instances (settings.allow_registration).

    Always creates a plain USER — ADMIN is reserved for the one-time setup
    owner so open registration can never mint an administrator.
    """
    if not http_request.app.state.settings.allow_registration:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="registration is disabled")
    async with uow:
        try:
            user = await uow.users.create(payload.email, http_request.app.state.auth.hash_password(payload.password), "USER")
            await uow.commit()
        except IntegrityError:
            # See setup_admin: unique-email conflict only, other errors 500.
            await uow.rollback()
            raise HTTPException(status_code=409, detail="email already registered") from None
        user_id, user_email, user_role, registered_at = user.id, user.email, user.role, user.created_at
    return {"id": user_id, "email": user_email, "role": user_role}


@router.get("/registration-status")
async def registration_status(
    http_request: Request,
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, bool]:
    """Public flags (no session required) so the login page can pick the right
    account entry: setup for a fresh instance, register when allowed, or plain
    login once initialized with registration disabled."""
    async with uow:
        initialized = (await uow.users.count()) > 0
    return {
        "enabled": bool(http_request.app.state.settings.allow_registration),
        "initialized": initialized,
    }


@router.post("/login")
async def login(
    payload: LoginRequest,
    http_request: Request,
    response: Response,
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
    _csrf: Annotated[None, Depends(require_same_origin)],
) -> dict[str, str]:
    async with uow:
        user = await uow.users.get_by_email(payload.email)
        if user is None:
            password_hash = None
            user_id = ""
            email = payload.email
            role = "USER"
            session_version = 1
        else:
            # Capture every attribute inside the session scope; the ORM
            # instance is detached once the unit of work exits.
            password_hash = user.password_hash
            user_id = user.id
            email = user.email
            role = user.role
            session_version = int(user.session_version or 1)
    if password_hash is None:
        password_hash = _dummy_password_hash(http_request.app.state.auth)
    if not http_request.app.state.auth.verify_password(payload.password, password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    from proseforge.application.auth.service import AuthUser
    token = http_request.app.state.auth.issue_token(AuthUser(user_id, email, role, session_version))
    # RFC 6265: the Secure flag must follow the scheme the cookie is actually
    # served over. Gating on environment instead would make browsers reject
    # the cookie on plain-HTTP (e.g. bare-IP) production deployments.
    is_public_https = http_request.app.state.settings.public_url.lower().startswith("https://")
    response.set_cookie("proseforge_session", token, httponly=True, secure=is_public_https, samesite="lax", max_age=http_request.app.state.settings.session_token_minutes * 60, path="/")
    return {"access_token": token, "token_type": "bearer"}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    _csrf: Annotated[None, Depends(require_same_origin)],
) -> None:
    # Logout only clears the shared session cookie; it is idempotent and
    # touches neither token state nor the database.
    #
    # Deliberate multi-account trade-off: logout does NOT bump
    # session_version. Tabs/devices share one account via per-tab bearer
    # tokens (the cookie can only pin one session per origin), so one tab
    # signing out must not kick the others. Password change remains the
    # revocation path — change_password below still bumps session_version.
    response.delete_cookie("proseforge_session", path="/")


@router.get("/me")
async def me(user: Annotated[AuthUser, Depends(current_user)]) -> dict[str, str]:
    return {"id": user.id, "email": user.email, "role": user.role}


@router.put("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: PasswordChangeRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    http_request: Request,
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
    _csrf: Annotated[None, Depends(require_same_origin)],
) -> None:
    async with uow:
        record = await uow.users.get_by_id(user.id)
        if record is None or not http_request.app.state.auth.verify_password(payload.current_password, record.password_hash):
            raise HTTPException(status_code=401, detail="invalid current password")
        record.password_hash = http_request.app.state.auth.hash_password(payload.new_password)
        record.session_version = int(record.session_version or 1) + 1
        await uow.commit()
