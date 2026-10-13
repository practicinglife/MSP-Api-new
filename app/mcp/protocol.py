"""MCP over streamable HTTP (stateless, JSON responses), shared by every MCP server this app serves.

Implemented directly on JSON-RPC 2.0 so it does not depend on the ``mcp`` package version. A server is
described by a ``Server`` (info, instructions, tools, resources) plus a tool-call function; ``respond`` turns
one HTTP POST (a message or a batch) into the reply.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse, Response

log = structlog.get_logger(__name__)

PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
MAX_TEXT = 80_000

# Every MCP endpoint: the MSP pack's /msp/mcp, the hub /mcp, one per connection, and groups of a connection.
MCP_PATH = r"(?:msp/mcp|mcp(?:/[a-z][a-z0-9_]{1,40}(?:/[a-z0-9][a-z0-9_-]{0,39})?)?)"
MCP_PATH_RE = re.compile(rf"^/{MCP_PATH}$")

ToolCall = Callable[[str, dict[str, Any]], Awaitable[tuple[bool, Any]]]
ResourceRead = Callable[[str], Awaitable[tuple[str, str] | None]]


@dataclass
class Server:
    name: str
    title: str
    version: str
    instructions: str
    tools: list[dict[str, Any]]
    call: ToolCall
    resources: list[dict[str, Any]] = field(default_factory=list)
    read_resource: ResourceRead | None = None


def rpc_error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def as_text(data: Any, hint: str = "Narrow the request (filters, a smaller page).") -> str:
    text = data if isinstance(data, str) else json.dumps(data, default=str, ensure_ascii=False, indent=1)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + f"\n… truncated ({len(text):,} chars total). {hint}"
    return text


def public_base_url(request: Request) -> str:
    """Base of the server URLs shown to people: MSP_PUBLIC_BASE_URL, else the configured external URL (the one
    OAuth tokens are issued for), else the address this request came in on."""
    from app.org_profile import EDGE_DOMAIN, external_base_url

    explicit = os.environ.get("MSP_PUBLIC_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    if os.environ.get("MSP_EXTERNAL_BASE_URL", "").strip() or EDGE_DOMAIN:
        return external_base_url()
    return str(request.base_url).rstrip("/")


async def handle(server: Server, msg: Any) -> dict[str, Any] | None:
    """One JSON-RPC message → its response (None for notifications)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    if "id" not in msg:
        return None
    method, mid = msg["method"], msg.get("id")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return rpc_error(mid, -32602, "params must be an object")
    try:
        if method == "initialize":
            requested = params.get("protocolVersion")
            caps: dict[str, Any] = {"tools": {"listChanged": False}}
            if server.resources:
                caps["resources"] = {"listChanged": False, "subscribe": False}
            result: dict[str, Any] = {
                "protocolVersion": requested if requested in PROTOCOLS else PROTOCOLS[1],
                "capabilities": caps,
                "serverInfo": {"name": server.name, "title": server.title, "version": server.version},
                "instructions": server.instructions,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": server.tools}
        elif method == "tools/call":
            name = params.get("name", "")
            if not any(t["name"] == name for t in server.tools):
                return rpc_error(mid, -32602, f"Unknown tool: {name}")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                ok, data = False, "arguments must be an object"
            else:
                ok, data = await server.call(name, args)
            result = {"content": [{"type": "text", "text": as_text(data)}], "isError": not ok}
        elif method == "resources/list":
            result = {"resources": server.resources}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        elif method == "resources/read":
            uri = str(params.get("uri", ""))
            found = await server.read_resource(uri) if server.read_resource else None
            if found is None:
                return rpc_error(mid, -32002, f"Resource not found: {uri}")
            mime, text = found
            result = {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}
        elif method == "prompts/list":
            result = {"prompts": []}
        else:
            return rpc_error(mid, -32601, f"Method not found: {method}")
    except Exception as exc:  # noqa: BLE001
        log.error("mcp.error", server=server.name, method=method, error=str(exc))
        return rpc_error(mid, -32603, f"Internal error: {exc}")
    return {"jsonrpc": "2.0", "id": mid, "result": result}


async def respond(request: Request, build: Callable[[], Awaitable[Server]]) -> Response:
    """Handle one streamable-HTTP POST. The server is built only when a message needs it."""
    # JSON only: a cross-site HTML form can't send it without a CORS preflight, which this app never grants.
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return JSONResponse(rpc_error(None, -32600, "Content-Type must be application/json"), status_code=415)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(rpc_error(None, -32700, "Parse error"), status_code=400)
    batch = isinstance(payload, list)
    messages = payload if batch else [payload]
    if batch and not messages:
        return JSONResponse(rpc_error(None, -32600, "Empty batch"), status_code=400)
    server: Server | None = None
    responses = []
    for m in messages:
        if isinstance(m, dict) and m.get("jsonrpc") == "2.0" and "method" in m and "id" not in m:
            continue  # notification
        if server is None:
            server = await build()
        r = await handle(server, m)
        if r is not None:
            responses.append(r)
    if not responses:
        return Response(status_code=202)
    return JSONResponse(responses if batch else responses[0])


def not_streaming() -> Response:
    """Stateless servers: no server-initiated SSE stream and no sessions to delete."""
    return Response(status_code=405, headers={"Allow": "POST"})
