from __future__ import annotations

import time
from collections import deque
from uuid import uuid4

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_AUTH_PREFIX = "/api/v1/auth/"
_AUTH_REGISTER_PATH = "/api/v1/auth/register"
# Failed authentications always count against the auth bucket. Register also
# counts created/duplicate accounts so an open instance cannot be flooded
# with throwaway users.
_AUTH_FAILURE_STATUSES = {401, 403}
_AUTH_REGISTER_COUNTED_STATUSES = {201, 409}


def _auth_attempt_counts(path: str, status_code: int) -> bool:
    if status_code in _AUTH_FAILURE_STATUSES:
        return True
    return path == _AUTH_REGISTER_PATH and status_code in _AUTH_REGISTER_COUNTED_STATUSES


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        supplied = request.headers.get("x-correlation-id", "")
        correlation_id = supplied[:128] if supplied and supplied.replace("-", "").isalnum() else uuid4().hex
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["x-correlation-id"] = correlation_id
        return response


class AgentRateLimitMiddleware(BaseHTTPMiddleware):
    """仅作用于 /api/v3/ 与 /api/v1/auth/ 的内存滑动窗口限流。

    按用户（会话 token 解出的 user_id，否则来源 IP）分桶，读写分别计数；
    其余 v1/v2 路由完全不受影响。内存实现随进程生命周期，多副本部署需换共享存储。

    auth 桶按失败计数：401/403（爆破、被拒的注册）必计，register 的
    201/409（灌账号、重复邮箱探测）也计；成功的 login 与 setup 稳态 409
    不计，正常登录与幂等 setup 探测不会被误伤。
    """

    def __init__(self, app, read_per_minute: int = 60, write_per_minute: int = 20, auth_per_minute: int = 10):
        super().__init__(app)
        self.read_limit = max(1, read_per_minute)
        self.write_limit = max(1, write_per_minute)
        self.auth_limit = max(1, auth_per_minute)
        self._hits: dict[tuple[str, str], deque[float]] = {}

    def _identity(self, request) -> str:
        token = ""
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        if not token:
            token = request.cookies.get("proseforge_session", "")
        if token:
            try:
                return f"user:{request.app.state.auth.decode_token(token).id}"
            except Exception:  # noqa: S110 -- invalid/expired token falls back to the IP key; best-effort identity, never blocks the request
                pass
        return f"ip:{request.client.host if request.client else 'unknown'}"

    def _window(self, key: tuple[str, str]) -> deque[float]:
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= 60.0:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
            hits = self._hits.setdefault(key, deque())
        return hits

    def _limited_response(self, request, hits: deque[float], message: str) -> JSONResponse:
        retry_after = max(1, int(60.0 - (time.monotonic() - hits[0])) + 1)
        return JSONResponse(
            status_code=429,
            content={"error": {"code": "RATE_LIMITED", "message": message, "retryable": True, "request_id": getattr(request.state, "correlation_id", ""), "details": {}}},
            headers={"Retry-After": str(retry_after)},
        )

    async def _limit_auth(self, request, call_next):
        key = (self._identity(request), "auth")
        hits = self._window(key)
        if len(hits) >= self.auth_limit:
            return self._limited_response(request, hits, "auth request rate limit exceeded")
        response = await call_next(request)
        if _auth_attempt_counts(request.url.path, response.status_code):
            hits.append(time.monotonic())
        return response

    async def dispatch(self, request, call_next):
        if request.url.path.startswith(_AUTH_PREFIX):
            return await self._limit_auth(request, call_next)
        if not request.url.path.startswith("/api/v3/"):
            return await call_next(request)
        kind = "write" if request.method in _WRITE_METHODS else "read"
        limit = self.write_limit if kind == "write" else self.read_limit
        key = (self._identity(request), kind)
        hits = self._window(key)
        if len(hits) >= limit:
            return self._limited_response(request, hits, "agent request rate limit exceeded")
        hits.append(time.monotonic())
        return await call_next(request)
