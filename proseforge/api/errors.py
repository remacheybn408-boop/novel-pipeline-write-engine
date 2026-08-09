"""Unified API error envelope for domain errors.

Maps the DomainError hierarchy (proseforge/domain/common/errors.py) onto
the canonical JSON envelope shared with the handwritten route/middleware
responses:

    {"error": {"code", "message", "retryable", "request_id", "details"}}

Status mapping follows the usual convention: NotFoundError -> 404,
ConflictError -> 409, ValidationError -> 422, ProviderError -> 502,
anything else -> 500. An exception class may pin a different status with
a ``status_code`` class attribute (e.g. a validation case that the API
has always answered with 400).
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from proseforge.domain.common.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    ProviderError,
    ValidationError,
)

_STATUS_BY_TYPE: tuple[tuple[type[DomainError], int], ...] = (
    (NotFoundError, 404),
    (ConflictError, 409),
    (ValidationError, 422),
    (ProviderError, 502),
)


def _status_for(exc: DomainError) -> int:
    pinned = getattr(exc, "status_code", None)
    if isinstance(pinned, int):
        return pinned
    for error_type, status in _STATUS_BY_TYPE:
        if isinstance(exc, error_type):
            return status
    return 500


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=_status_for(exc),
        content={
            "error": {
                "code": exc.code,
                "message": str(exc),
                "retryable": exc.retryable,
                "request_id": getattr(request.state, "correlation_id", ""),
                "details": {},
            }
        },
    )
