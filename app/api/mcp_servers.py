"""MCP endpoints for connections (streamable HTTP, stateless JSON).

* ``POST /mcp``                  — hub across every connection the caller may use
* ``POST /mcp/{slug}``           — one connection as its own MCP server
* ``POST /mcp/{slug}/{group}``   — a hand-picked group of a connection's operations
* ``GET  /api/mcp/servers``      — the servers this caller can use, with ready-to-paste client configs
* ``GET  /mcp/bridge.py``        — stdio bridge for clients that only start local servers (no secrets inside)

Authentication is the middleware's (session, connection key or OAuth token). An OAuth token is valid only for
the one server it was issued for (``resource``), so a token for ``/mcp/github`` is refused at ``/mcp/jira``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.auth.principal import Principal, get_current
from app.mcp import protocol
from app.mcp.servers import (
    Caller,
    Unavailable,
    connection_for,
    connection_server,
    groups_of,
    hub_server,
    usable_connections,
)

router = APIRouter(tags=["mcp"])

_BRIDGE = Path(__file__).resolve().parent.parent / "mcp" / "stdio_bridge.py"
HUB_NAME = "apis"


def _principal(request: Request) -> Principal:
    p = getattr(request.state, "principal", None) or get_current()
    if p is None:  # the middleware refuses these first; defence in depth
        raise HTTPException(401, "Sign-in required")
    return p


def _caller(request: Request) -> Caller:
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else None)
    return Caller(principal=_principal(request), path=request.url.path, ip=ip,
                  base_url=protocol.public_base_url(request))


@router.get("/mcp/bridge.py", include_in_schema=False)
async def bridge_script() -> PlainTextResponse:
    return PlainTextResponse(_BRIDGE.read_text(encoding="utf-8"),
                             headers={"Content-Disposition": "attachment; filename=mcp-bridge.py"})


@router.post("/mcp")
async def hub(request: Request) -> Response:
    caller = _caller(request)
    return await protocol.respond(request, lambda: hub_server(caller))


@router.post("/mcp/{slug}")
async def connection_mcp(slug: str, request: Request) -> Response:
    return await _serve(request, slug, None)


@router.post("/mcp/{slug}/{group}")
async def group_mcp(slug: str, group: str, request: Request) -> Response:
    return await _serve(request, slug, group)


async def _serve(request: Request, slug: str, group: str | None) -> Response:
    caller = _caller(request)
    try:
        conn = await connection_for(slug, caller.principal, group)
    except Unavailable as exc:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)
    return await protocol.respond(request, lambda: connection_server(conn, caller, group))


@router.api_route("/mcp", methods=["GET", "DELETE"], include_in_schema=False)
@router.api_route("/mcp/{slug}", methods=["GET", "DELETE"], include_in_schema=False)
@router.api_route("/mcp/{slug}/{group}", methods=["GET", "DELETE"], include_in_schema=False)
async def not_streaming() -> Response:
    return protocol.not_streaming()


def client_configs(name: str, url: str, base: str) -> dict[str, Any]:
    """Copy-paste setups for common MCP clients. ``<connection key>`` is a key from an admin (msp_…)."""
    key = "<your connection key>"
    return {
        "claude_code": f'claude mcp add --transport http {name} {url} --header "X-API-Key: {key}"',
        "vscode": {"inputs": [{"id": "msp-key", "type": "promptString", "description": "Connection key",
                               "password": True}],
                   "servers": {name: {"type": "http", "url": url, "headers": {"X-API-Key": "${input:msp-key}"}}}},
        "cursor": {"mcpServers": {name: {"url": url, "headers": {"X-API-Key": key}}}},
        "claude_desktop": {"mcpServers": {name: {"command": "python",
                                                 "args": ["<folder>/mcp-bridge.py", url],
                                                 "env": {"MSP_API_KEY": key}}}},
        "bridge_download": f"{base}/mcp/bridge.py",
        "claude_ai_connector": url,
    }


@router.get("/api/mcp/servers")
async def list_servers(request: Request) -> dict[str, Any]:
    """The MCP servers this caller may use: the hub, one per connection, and each connection's groups."""
    principal = _principal(request)
    base = protocol.public_base_url(request)
    servers = []
    for c in await usable_connections(principal):
        url = f"{base}/mcp/{c.slug}"
        servers.append({"connection": c.slug, "name": c.name, "url": url,
                        "configs": client_configs(c.slug, url, base),
                        "groups": [{"group": g, "operations": ops, "url": f"{url}/{g}"}
                                   for g, ops in groups_of(c).items()]})
    hub_url = f"{base}/mcp"
    return {"hub": {"name": HUB_NAME, "url": hub_url, "configs": client_configs(HUB_NAME, hub_url, base)},
            "servers": servers,
            "note": "Each client needs a connection key (an admin creates one), or signs in with OAuth where the "
                    "client supports it (claude.ai connectors)."}
