"""Who is calling, and what they may do.

Every request (LAN or internet) resolves to a ``Principal`` or is refused by the middleware in
``app/security.py``. Sources, in order:

1. In-process calls the app makes to itself (MCP tools, workflow replays) — ``X-MSP-Internal`` carries a
   per-install secret plus the *original* caller, so identity and permissions pass through unchanged.
2. Browser session cookie (OIDC / Microsoft 365 or local account sign-in).
3. ``Authorization: Bearer`` / ``X-API-Key`` — a connection key (``msp_…``) or an OAuth access token.

Roles (Entra app roles or IdP group mapping): User (read + propose), Editor (writes), Approver (final approval,
connector credentials), Admin (everything incl. keys, policies, audit). Scopes ``<platform|*>:<read|write>``
narrow API keys and OAuth tokens to particular platforms.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

ROLE_USER = "MSPTools.User"
ROLE_EDITOR = "MSPTools.Editor"
ROLE_APPROVER = "MSPTools.Approver"
ROLE_ADMIN = "MSPTools.Admin"
ALL_ROLES = (ROLE_USER, ROLE_EDITOR, ROLE_APPROVER, ROLE_ADMIN)

READ, WRITE = "read", "write"
INTERNAL_HEADER = "x-msp-internal"
INTERNAL_PRINCIPAL_HEADER = "x-msp-principal"


def _internal_secret() -> str:
    """Stable across workers when a master key exists (derived), otherwise random per process."""
    raw = os.environ.get("MSP_MASTER_KEY", "").strip()
    if raw:
        return hmac.new(raw.encode(), b"msp-internal-call-v1", hashlib.sha256).hexdigest()
    return secrets.token_urlsafe(32)


INTERNAL_SECRET = _internal_secret()


@dataclass(frozen=True)
class Principal:
    id: str                       # upn / "key:<name>" / "local:<user>"
    kind: str                     # session | api_key | oauth | internal
    roles: frozenset[str] = field(default_factory=frozenset)
    scopes: frozenset[str] = field(default_factory=frozenset)
    name: str = ""
    csrf: str | None = None
    resource: str | None = None   # OAuth audience (MCP server URL)
    via: str | None = None        # for internal calls: "mcp" / "workflow"

    # ── roles ──
    def has_role(self, *roles: str) -> bool:
        if ROLE_ADMIN in self.roles:
            return True
        return any(r in self.roles for r in roles)

    @property
    def is_admin(self) -> bool:
        return ROLE_ADMIN in self.roles

    @property
    def can_edit(self) -> bool:
        return self.has_role(ROLE_EDITOR, ROLE_APPROVER)

    # ── scopes ──
    def allows(self, platform: str, action: str) -> bool:
        wanted = {f"{platform}:{action}", f"*:{action}"}
        if action == READ:  # write implies read
            wanted |= {f"{platform}:{WRITE}", f"*:{WRITE}"}
        return bool(self.scopes & wanted)

    def can_write(self, platform: str = "*") -> bool:
        return self.can_edit and self.allows(platform, WRITE)

    # ── display / transport ──
    @property
    def source(self) -> str:
        if self.kind == "internal":
            return self.via or "internal"
        return {"session": "ui", "oauth": "mcp"}.get(self.kind, "api")

    def label(self) -> str:
        return f"{self.id} via {self.via}" if self.kind == "internal" and self.via else self.id

    def to_header(self) -> str:
        data = {"id": self.id, "kind": self.kind, "roles": sorted(self.roles), "scopes": sorted(self.scopes),
                "name": self.name, "resource": self.resource}
        return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()


def scopes_for_roles(roles: frozenset[str] | set[str] | list[str]) -> frozenset[str]:
    """Default scopes for interactive users: everyone reads, editors write (still gated by write mode)."""
    r = set(roles)
    if r & {ROLE_EDITOR, ROLE_APPROVER, ROLE_ADMIN}:
        return frozenset({"*:read", "*:write"})
    return frozenset({"*:read"})


def internal_headers(principal: Principal, via: str) -> dict[str, str]:
    """Headers for an in-process call made on behalf of ``principal`` (MCP tool, workflow step)."""
    return {"X-MSP-Internal": INTERNAL_SECRET, "X-MSP-Principal": principal.to_header(), "X-MSP-Via": via}


def from_internal_headers(headers: dict[str, str]) -> Principal | None:
    token = headers.get(INTERNAL_HEADER, "")
    if not token or not hmac.compare_digest(token, INTERNAL_SECRET):
        return None
    try:
        data: dict[str, Any] = json.loads(base64.urlsafe_b64decode(headers.get(INTERNAL_PRINCIPAL_HEADER, "")))
    except (ValueError, TypeError):
        return None
    return Principal(
        id=str(data.get("id") or "internal"), kind="internal",
        roles=frozenset(data.get("roles") or []), scopes=frozenset(data.get("scopes") or []),
        name=str(data.get("name") or ""), resource=data.get("resource"),
        via=(headers.get("x-msp-via") or "internal")[:32],
    )


# Set by the middleware for the duration of each request, so deep code (write-mode checks, change requests)
# knows the caller without threading it through every function signature.
current_principal: ContextVar[Principal | None] = ContextVar("current_principal", default=None)


def get_current() -> Principal | None:
    return current_principal.get()


SYSTEM = Principal(id="system", kind="internal", roles=frozenset({ROLE_ADMIN}),
                   scopes=frozenset({"*:read", "*:write"}), via="system")


async def resolve(scope: dict[str, Any], headers: dict[str, str]) -> Principal | None:
    """Resolve the caller of an ASGI request, or None if it carries no valid credential."""
    p = from_internal_headers(headers)
    if p is not None:
        return p

    from starlette.requests import Request

    request = Request(scope)
    from app.api.auth_m365 import get_session

    s = await get_session(request)
    if s is not None:
        roles = frozenset(r for r in (s.roles or []) if r in ALL_ROLES)
        return Principal(id=s.upn, kind="session", roles=roles, scopes=scopes_for_roles(roles),
                         name=s.name or s.upn, csrf=s.csrf)

    from app.api.external_auth import principal_from_token

    return await principal_from_token(request)
