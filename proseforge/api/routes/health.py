from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from proseforge.infrastructure.security.credential_cipher import (
    CredentialCipher,
    derive_key,
)
from proseforge.operations.startup_check import read_ready_check
from proseforge.runtime.profile import RuntimeProfile, capabilities_for

router = APIRouter(prefix="/api/v1/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/time")
async def server_time() -> dict[str, object]:
    # Authoritative server clock (host is NTP-synced); clients offset their own
    # clock against epoch_ms instead of trusting the local clock.
    utc_now = datetime.now(UTC)
    local_now = utc_now.astimezone()
    return {
        "utc_now": utc_now.isoformat(),
        "epoch_ms": int(utc_now.timestamp() * 1000),
        "local_now": local_now.isoformat(),
        "timezone": local_now.tzname() or "unknown",
    }


@router.get("/ready")
async def ready(request: Request) -> dict[str, object]:
    report = read_ready_check(request.app.state.settings.blob_root, request.app.state.settings.backup_root)
    checks = {"api": "ok", **report.checks}
    master_key = request.app.state.settings.master_key.get_secret_value()
    if master_key.startswith("replace-with-") and request.app.state.settings.environment.lower() not in {"production", "prod"}:
        checks["master_key"] = "ok"
    else:
        try:
            CredentialCipher(derive_key(master_key))
            checks["master_key"] = "ok"
        except (ValueError, TypeError):
            checks["master_key"] = "error"
    profile = RuntimeProfile(request.app.state.settings.runtime_profile)
    native = capabilities_for(profile).database == "sqlite"
    lifecycle = getattr(request.app.state, "lifecycle", None)
    lifecycle_started = bool(getattr(lifecycle, "_started", False))
    if not lifecycle_started and hasattr(request.app.state, "lifecycle"):
        checks["lifecycle"] = "error"
        return JSONResponse(status_code=503, content={"status": "not_ready", "checks": checks})
    try:
        async with request.app.state.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            migration = await connection.scalar(text("SELECT version_num FROM alembic_version ORDER BY version_num DESC LIMIT 1"))
            if native:
                workflow_table = await connection.scalar(text("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'workflow_runs'"))
                pgvector = True
                partial_messages = await connection.scalar(text("SELECT count(*) FROM messages WHERE status = 'PARTIAL'"))
                expired_workflows = 0
            else:
                workflow_table = await connection.scalar(text("SELECT to_regclass('workflow_runs')"))
                pgvector = await connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"))
                partial_messages = await connection.scalar(text("SELECT count(*) FROM messages WHERE status = 'PARTIAL'"))
                expired_workflows = await connection.scalar(text("SELECT count(*) FROM workflow_runs WHERE status IN ('RUNNING', 'RECOVERING') AND lease_expires_at IS NOT NULL AND lease_expires_at < now()"))
        checks["database"] = "ok"
        checks["migration"] = "ok" if migration else "error"
        checks["workflow_recovery"] = "ok" if workflow_table else "error"
        checks["pgvector"] = "ok" if pgvector else "error"
        checks["partial_messages"] = "ok" if (partial_messages or 0) >= 0 else "error"
        checks["expired_workflows"] = "ok" if (expired_workflows or 0) == 0 else "warning"
    except Exception:
        checks["database"] = "error"
        checks["migration"] = "error"
        checks["workflow_recovery"] = "error"
        checks["pgvector"] = "error"
        checks["partial_messages"] = "error"
        checks["expired_workflows"] = "error"
    if native:
        checks["redis"] = "not_applicable"
    else:
        redis_client = Redis.from_url(request.app.state.settings.redis_url)
        try:
            await redis_client.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "error"
        finally:
            await redis_client.aclose()
    ready_state = all(value in {"ok", "warning", "not_applicable"} for value in checks.values())
    payload = {"status": "ready" if ready_state else "not_ready", "checks": checks}
    if not ready_state:
        return JSONResponse(status_code=503, content=payload)
    return payload


@router.get("/report")
async def report(request: Request) -> dict[str, object]:
    return await ready(request)


@router.post("/run")
async def run(request: Request) -> dict[str, object]:
    return await report(request)
