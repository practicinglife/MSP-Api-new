"""Network boundary + authentication + authorization + audit (pure ASGI middleware).

Boundary — anything from the internet must come through the nginx edge (``X-MSP-Edge: 1``):
* Host header that is not a LAN address / LAN hostname / the edge domain   → 403 (tunnels)
* X-Forwarded-For containing a public IP without the edge header            → 403
* Edge traffic from **office/LAN/VPN** sources: full app (dashboard, /changes, sign-in)
* Edge traffic from anyone else (e.g. Anthropic): only MCP + OAuth discovery/register/token → else 404

Authentication — **every** route needs a caller (session cookie, connection key or OAuth token), on the LAN
too. Only health, sign-in and OAuth discovery/registration/token are public. Browsers without a session are
sent to ``/auth/login``; everything else gets 401.

Authorization — four roles, each including the ones before it (``app/auth/principal.py``):
* **Reader**: everything that only reads, calling read-only operations, and proposals (they change nothing
  until approved).
* **Editor**: writes (plus a ``write`` scope for keys/tokens, and the platform's write mode) and submitting
  change requests.
* **Approver**: final approval of change requests; connections and their credentials, built-in integrations,
  local copies, write modes, connection import/export.
* **Admin**: the rest of ``/api/admin/*`` and ``/admin/*`` — users, keys, sign-in, certificates, packs, audit.
* OAuth access tokens are bound to the MCP server they were issued for (RFC 8707 audience).
* Direct writes to a vendor additionally pass the platform's write mode (``app/auth/write_mode.py``).
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections import deque
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlparse

import structlog

from app.auth import audit, ratelimit
from app.auth.principal import ROLE_ADMIN, ROLE_APPROVER, Principal, current_principal, resolve
from app.mcp.protocol import MCP_PATH, MCP_PATH_RE
from app.org_profile import EDGE_DOMAIN, external_base_url, OFFICE_IPS

log = structlog.get_logger(__name__)

_INTERNAL_HOSTS = {"msp.internal", "testserver", "localhost"}
# Paths Anthropic (any non-office edge source) may reach: the MCP servers and OAuth discovery / registration /
# token. /oauth/authorize is a browser page → office only.
_PUBLIC_EDGE_PATHS = re.compile(
    rf"^/({MCP_PATH}|\.well-known/oauth-(protected-resource|authorization-server)(/{MCP_PATH})?"
    r"|oauth/(register|token))$"
)
# Reachable without credentials (each does its own checks).
_PUBLIC_PATHS = re.compile(
    r"^/(health|favicon\.ico|auth/(login|callback|logout|me|local)|setup|setup/(status|admin)"
    r"|oauth/(register|token|authorize)|\.well-known/oauth-(protected-resource|authorization-server)(/.*)?"
    # installer / bridge scripts: no data or secrets inside — they ask the user for a connection key themselves
    r"|msp/plugin/(install\.ps1|install\.sh|install-desktop\.ps1|msp-bridge\.ps1)|mcp/bridge\.py)$"
)
# Unauthenticated endpoints that take guesses (passwords, codes) — tighter per-IP limit.
_ANON_LIMITED = re.compile(r"^/(auth/local|setup/admin|oauth/(token|authorize|register))$")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Writes any signed-in caller may make: they only *propose* (nothing runs until an Editor and an Approver act), or
# the route enforces its own role + CSRF checks (submit/approve/reject).
_PROPOSE_ONLY = re.compile(
    r"^/api/(changes|changes/[^/]+/(submit|approve|reject|cancel)|av/sweep|helpdesk/s1-incidents/propose"
    r"|tickets/triage|agent/request|agent/workflows/[^/]+/replay)$"
)
_ADMIN_PATHS = re.compile(r"^/(api/)?admin(/|$)")
# Admin-area pages an Approver may use: they're about connections, not about people or the server
_ADMIN_FOR_APPROVERS = re.compile(r"^/api/admin/(write-modes(/[^/]+)?|connections/(export|import))$")
_APPROVER_ONLY = re.compile(
    r"^/api/(connectors/(register|configs(/.*)?)"
    # connections: create / edit / delete / re-import / operation flags / local-copy syncs
    r"|connections(/[^/]+(/reimport|/operations/[^/]+|/syncs(/[^/]+(/run)?)?)?)?)$"
)
# Calls check read scope, write mode and write scope per operation (app/connections/service.py) and audit
# themselves, so the generic "non-GET needs Editor" rule doesn't apply.
_SELF_CHECKED = re.compile(r"^/api/connections/[^/]+/operations/[^/]+/call$")
_MCP_PATHS = MCP_PATH_RE  # MCP servers audit each tool call themselves

# Recent requests that came through the internet edge (for troubleshooting by admins).
EDGE_LOG: deque[dict[str, Any]] = deque(maxlen=200)


def _is_private_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def is_internal_source(ip: str | None) -> bool:
    return bool(ip) and (_is_private_ip(ip) or ip.strip() in OFFICE_IPS)


def _host_allowed(host: str) -> bool:
    host = host.strip().lower()
    if host.startswith("["):  # [ipv6]:port
        name = host[1:].split("]", 1)[0]
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if not name:
        return False
    if (EDGE_DOMAIN and name == EDGE_DOMAIN) or name in _INTERNAL_HOSTS:
        return True
    if _is_private_ip(name):
        return True
    # single-label LAN names (myserver, app, ...) — public tunnels always use FQDNs
    return "." not in name and bool(re.fullmatch(r"[a-z0-9-]+", name))


def _mcp_resource_path(principal: Principal) -> str:
    """Path of the MCP server an OAuth token was issued for (tokens from before binding → /msp/mcp)."""
    resource = principal.resource or f"{external_base_url()}/msp/mcp"
    return urlparse(resource).path.rstrip("/") or "/"


class NetworkBoundaryMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        edge = headers.get("x-msp-edge") == "1"
        src = headers.get("x-real-ip") or (scope.get("client") or ("", 0))[0]
        if edge:
            EDGE_LOG.append({
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "ip": src,
                "method": method,
                "path": path,
                "auth": "bearer" if headers.get("authorization", "").lower().startswith("bearer ")
                        else ("x-api-key" if headers.get("x-api-key") else "none"),
                "user_agent": headers.get("user-agent", "")[:120],
            })

        # ── 1. network boundary ──
        reason = None
        if not _host_allowed(headers.get("host", "")):
            reason = "host_not_allowed"
        elif not edge:
            xff = headers.get("x-forwarded-for", "")
            if xff and any(not _is_private_ip(p) for p in xff.split(",") if p.strip()):
                reason = "forwarded_from_public_ip"
        elif headers.get("x-msp-internal"):
            reason = "internal_header_from_edge"
        elif not is_internal_source(src) and not _PUBLIC_EDGE_PATHS.match(path):
            await _respond(send, 404, {"detail": "Not Found"})
            return
        if reason:
            client = scope.get("client") or ("?", 0)
            log.warning("security.blocked", reason=reason, host=headers.get("host"), path=path,
                        xff=headers.get("x-forwarded-for"), client=client[0])
            await _respond(send, 403, {"detail": "Forbidden"})
            return

        # ── 2. public routes ──
        if _PUBLIC_PATHS.match(path):
            if method == "POST" and _ANON_LIMITED.match(path) \
                    and not ratelimit.allow(f"ip:{src}:{path}", ratelimit.anonymous_limit()):
                await _respond(send, 429, {"detail": "Too many requests — try again in a minute"})
                return
            await self.app(scope, receive, send)
            return

        # ── 3. authentication ──
        principal = await resolve(scope, headers)
        if principal is None:
            await _unauthenticated(send, method, path, headers)
            return
        if principal.kind == "oauth" and path != _mcp_resource_path(principal):
            await _respond(send, 403, {"detail": "This OAuth token is not valid for this resource"})
            return
        if principal.kind != "internal" and not ratelimit.allow(f"p:{principal.id}", ratelimit.per_principal_limit()):
            await _respond(send, 429, {"detail": "Rate limit exceeded"})
            return

        # ── 4. authorization ──
        denied = _authorize(principal, method, path, headers)
        if denied:
            status, detail = denied
            log.warning("security.denied", path=path, method=method, status=status, who=principal.id)
            await _respond(send, status, {"detail": detail})
            await _audit(principal, method, path, status, src)
            return

        # ── 5. run, then audit ──
        scope.setdefault("state", {})["principal"] = principal
        token = current_principal.set(principal)
        status_holder = {"status": 500}

        async def _send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            current_principal.reset(token)
            if audit.should_audit(method, path) and not _MCP_PATHS.match(path) and not _SELF_CHECKED.match(path):
                await _audit(principal, method, path, status_holder["status"], src)


def _authorize(principal: Principal, method: str, path: str, headers: dict[str, str]) -> tuple[int, str] | None:
    if _ADMIN_PATHS.match(path):
        needed = ROLE_APPROVER if _ADMIN_FOR_APPROVERS.match(path) else ROLE_ADMIN
        if not principal.has_role(needed):
            return 403, f"Requires the {needed} role"
    if method in _SAFE_METHODS:
        return None
    api_path = path.startswith(("/api/", "/admin/"))
    if principal.kind == "session" and (api_path or _MCP_PATHS.match(path)):
        # CSRF: a browser POST from another site carries an Origin that doesn't match our host.
        origin = headers.get("origin")
        if origin and (urlparse(origin).netloc or "").lower() != headers.get("host", "").lower():
            return 403, "Cross-site request refused"
    if not api_path:
        return None
    if _APPROVER_ONLY.match(path) and not principal.has_role(ROLE_APPROVER):
        return 403, "Connections, credentials and integration settings require the Approver role"
    if _PROPOSE_ONLY.match(path) or _ADMIN_PATHS.match(path) or _SELF_CHECKED.match(path):
        return None
    if not principal.can_edit:
        return 403, "Requires the Editor role"
    if not any(s.endswith(":write") for s in principal.scopes):
        return 403, "This credential is read-only"
    return None


async def _audit(principal: Principal, method: str, path: str, status: int, ip: str | None) -> None:
    await audit.record(principal=principal.label(), auth_kind=principal.kind, source=principal.source,
                       method=method, path=path, status=status, ip=ip)


async def _unauthenticated(send: Any, method: str, path: str, headers: dict[str, str]) -> None:
    is_mcp = bool(_MCP_PATHS.match(path))
    wants_html = method == "GET" and "text/html" in headers.get("accept", "") \
        and not path.startswith(("/api/", "/msp/")) and not is_mcp
    if wants_html:
        location = f"/auth/login?next={quote(path, safe='/')}"
        await send({"type": "http.response.start", "status": 302,
                    "headers": [(b"location", location.encode()), (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})
        return
    # MCP clients discover the OAuth server from the metadata of the exact server they called (RFC 9728).
    www = f'Bearer resource_metadata="{external_base_url()}/.well-known/oauth-protected-resource{path if is_mcp else ""}"'
    await _respond(send, 401, {"detail": "Sign-in required: a session, connection key or OAuth token"},
                   extra=[(b"www-authenticate", www.encode())])


async def _respond(send: Any, status: int, body: dict[str, Any],
                   extra: list[tuple[bytes, bytes]] | None = None) -> None:
    payload = json.dumps(body).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()),
                    *(extra or [])],
    })
    await send({"type": "http.response.body", "body": payload})
