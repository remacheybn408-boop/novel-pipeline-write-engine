"""最小 streamable-http MCP client（probe 用，不引 MCP SDK）。

POST JSON-RPC：initialize（protocolVersion 2025-03-26）→ tools/list。
Accept: application/json, text/event-stream；服务端可能用 SSE 帧
（``data: {...}\\n\\n``）包裹响应，``parse_jsonrpc_body`` 两种都兼容。
任何网络/协议错误都由 probe_mcp_server 收敛为 {"ok": False, "error": ...}，
绝不向调用方抛异常（probe 是探活语义，失败即不可用）。
"""

from __future__ import annotations

import json

import httpx

PROBE_TIMEOUT_SECONDS = 10.0
_PROTOCOL_VERSION = "2025-03-26"


def parse_jsonrpc_body(body: str, content_type: str) -> dict:
    """从响应体提取单个 JSON-RPC 消息；SSE 帧取最后一条 data: JSON。"""
    if "text/event-stream" in content_type.lower():
        last: dict | None = None
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            last = json.loads(payload)
        if last is None:
            raise ValueError("SSE response contained no data frame")
        return last
    return json.loads(body)


def _jsonrpc(request_id: int, method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


async def probe_mcp_server(url: str, transport: str, headers: dict[str, str] | None = None) -> dict[str, object]:
    """initialize + tools/list 探活；返回 {"ok": True, "tools": [名称...]} 或 {"ok": False, "error": str}。"""
    request_headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **(headers or {})}
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            initialize = await client.post(url, headers=request_headers, json=_jsonrpc(1, "initialize", {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "proseforge", "version": "1.0"},
            }))
            initialize.raise_for_status()
            session_id = initialize.headers.get("mcp-session-id")
            if session_id:
                request_headers["mcp-session-id"] = session_id
            listing = await client.post(url, headers=request_headers, json=_jsonrpc(2, "tools/list", {}))
            listing.raise_for_status()
            message = parse_jsonrpc_body(listing.text, listing.headers.get("content-type", ""))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    if "error" in message:
        return {"ok": False, "error": str(message["error"])[:300]}
    result = message.get("result")
    tools = result.get("tools", []) if isinstance(result, dict) else []
    names = [str(tool.get("name", "")) for tool in tools if isinstance(tool, dict) and tool.get("name")]
    return {"ok": True, "tools": names}
