"""Who is calling, and what they may do.

Every request (LAN or internet) resolves to a ``Principal`` or is refused by the middleware in
``app/security.py``. Sources, in order:

1. In-process calls the app makes to itself (MCP tools, workflow replays) — ``X-MSP-Internal`` carries a
   per-install secret plus the *original* caller, so identity and permissions pass through unchanged.
2. Browser session cookie (OIDC / Microsoft 365 or local account sign-in).
3. ``Authorization: Bearer`` / ``X-API-Key`` — a connection key (``msp_…``) or an OAuth access token.

Four roles, each including the ones before it (Entra app roles, IdP group mapping, local accounts, keys):

* **Reader** — sign in, search and list connections, call read-only API operations, propose changes.
* **Editor** — plus run writes (where a connection's write mode is 'direct') and submit change requests.
* **Approver** — plus give final approval to change requests, and manage connections, credentials, local copies
  and write modes. An Editor submits, an Approver approves: two people for every approval-mode write.
* **Admin** — everything, plus users, connection keys, sign-in, certificates, packs and the audit log.

Older role names are still accepted: ``MSPTools.User`` → Reader, ``MSPTools.Editor`` → Editor,
``MSPTools.Approver`` → Approver, ``MSPTools.Admin`` → Admin. Scopes ``<platform|*>:<read|write>`` narrow keys and OAuth tokens further.
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

ROLE_READER = "Reader"
ROLE_EDITOR = "Editor"
ROLE_APPROVER = "Approver"
ROLE_ADMIN = "Admin"
ALL_ROLES = (ROLE_READER, ROLE_EDITOR, ROLE_APPROVER, ROLE_ADMIN)
RANK = {ROLE_READER: 1, ROLE_EDITOR: 2, ROLE_APPROVER: 3, ROLE_ADMIN: 4}
# Older names (Entra app roles, keys and sessions made before the three-role model) and other spellings
LEGACY_ROLES = {
    "MSPTools.User": ROLE_READER, "MSPTools.Reader": ROLE_READER, "User": ROLE_READER, "Viewer": ROLE_READER,
    "MSPTools.Editor": ROLE_EDITOR, "MSPTools.Approver": ROLE_APPROVER,
    "MSPTools.Admin": ROLE_ADMIN, "Administrator": ROLE_ADMIN,
}
ROLE_USER = ROLE_READER  # the older name


# What each role may do — the one list shown in Get started, /setup and the docs. tests/test_roles.py checks every
# row against the real endpoints, so this is exactly what is enforced (app/security.py, write modes, change gates).
ACCESS: list[dict[str, str]] = [
    {"what": "Sign in, search and list connections and their operations", "role": ROLE_READER},
    {"what": "Call read-only API operations (and read local copies) on the connections they may use", "role": ROLE_READER},
    {"what": "Propose a write: it becomes a change request and runs only once approved", "role": ROLE_READER},
    {"what": "Call write API operations, on connections whose write mode is 'direct'", "role": ROLE_EDITOR},
    {"what": "Submit change requests for approval (approval-mode writes)", "role": ROLE_EDITOR},
    {"what": "Give final approval to change requests (or reject them)", "role": ROLE_APPROVER},
    {"what": "Add, edit and delete connections, credentials, built-in integrations and local copies", "role": ROLE_APPROVER},
    {"what": "Set write modes; export and import connection settings", "role": ROLE_APPROVER},
    {"what": "Users, connection keys, sign-in, certificates, packs, setup and the audit log", "role": ROLE_ADMIN},
]
WRITE_MODE_NOTE = ("Every new connection starts read-only: no one, not even an Admin, can write through it until an "
                   "Approver sets its write mode to 'direct' or 'approval'. Connection keys can be Reader or Editor "
                   "(never Approver or Admin: approvals need a person) and are limited further by their scopes.")


def normalize_role(role: str) -> str | None:
    """The current name of ``role`` (case-insensitive, old names accepted), or None if it isn't one of ours."""
    r = str(role or "").strip()
    if r in RANK:
        return r
    if r in LEGACY_ROLES:
        return LEGACY_ROLES[r]
    low = r.lower()
    for name in RANK:
        if name.lower() == low:
            return name
    for old, new in LEGACY_ROLES.items():
        if old.lower() == low:
            return new
    return None


def normalize_roles(roles: Any) -> frozenset[str]:
    return frozenset(n for n in (normalize_role(r) for r in (roles or [])) if n)


def role_rank(roles: Any) -> int:
    return max((RANK[r] for r in normalize_roles(roles)), default=0)


def top_role(roles: Any) -> str:
    """The highest role held ("" for none): what the UI shows."""
    rank = role_rank(roles)
    return next((r for r, n in RANK.items() if n == rank), "")

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

    def __post_init__(self) -> None:  # every source of roles (sessions, keys, tokens, headers) ends up here
        object.__setattr__(self, "roles", normalize_roles(self.roles))

    # ── roles (each includes the ones below it) ──
    def has_role(self, *roles: str) -> bool:
        """At least the lowest of ``roles``: has_role(ROLE_APPROVER) is true for Approvers and Admins."""
        wanted = [RANK[n] for n in (normalize_role(r) for r in roles) if n]
        return bool(wanted) and role_rank(self.roles) >= min(wanted)

    @property
    def is_admin(self) -> bool:
        return self.has_role(ROLE_ADMIN)

    @property
    def can_edit(self) -> bool:
        """May run writes and submit changes (still subject to each connection's write mode and write scopes)."""
        return self.has_role(ROLE_EDITOR)

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
    """Default scopes for people signing in: Readers read; Editors and up may also write (still gated by each
    connection's write mode)."""
    if role_rank(roles) >= RANK[ROLE_EDITOR]:
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
        roles = normalize_roles(s.roles)
        return Principal(id=s.upn, kind="session", roles=roles, scopes=scopes_for_roles(roles),
                         name=s.name or s.upn, csrf=s.csrf)

    from app.api.external_auth import principal_from_token

    return await principal_from_token(request)
