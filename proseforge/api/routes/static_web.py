from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter(tags=["web"])


def _frontend_dir(request: Request) -> Path:
    settings = request.app.state.settings
    if not settings.serve_web or not settings.frontend_dir:
        raise HTTPException(status_code=404, detail="web hosting is disabled")
    root = Path(settings.frontend_dir).resolve()
    if not root.is_dir():
        raise HTTPException(status_code=503, detail="frontend assets are unavailable")
    return root


def _safe_asset(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="asset not found") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="asset not found")
    return candidate


@router.get("/runtime-config.json")
async def runtime_config(request: Request) -> JSONResponse:
    _frontend_dir(request)
    return JSONResponse(
        {
            "api_base_url": "/api",
            "profile": request.app.state.runtime.info["profile"],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/")
async def index(request: Request) -> FileResponse:
    root = _frontend_dir(request)
    return FileResponse(_safe_asset(root, "index.html"), headers={"Cache-Control": "no-cache"})


@router.get("/{path:path}")
async def spa_or_asset(request: Request, path: str) -> FileResponse:
    root = _frontend_dir(request)
    candidate = root / path
    if candidate.is_file():
        return FileResponse(_safe_asset(root, path))
    return FileResponse(_safe_asset(root, "index.html"), headers={"Cache-Control": "no-cache"})
