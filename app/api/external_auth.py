"""Machine access: connection keys + a minimal OAuth 2.1 authorization server.

Every request needs a credential (see ``app/security.py``). For programs and AI clients:
* ``Authorization: Bearer msp_…`` or ``X-API-Key: msp_…`` — a connection key (Claude Code, Copilot, scripts).
  Keys carry roles + scopes (``<platform|*>:<read|write>``) and an optional expiry; only admins create them.
* ``Authorization: Bearer <access token>`` from the OAuth flow below (claude.ai connectors). Tokens are bound
  to the MCP server (``resource``, RFC 8707) they were issued for and carry the signer's roles.

OAuth: Dynamic Client Registration, authorization code + PKCE (S256), rotating refresh tokens. With single
sign-on configured people approve with their own account; otherwise the page asks for a connection key.
Revoking a key kills every token issued from it.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, select

from app.auth.principal import ROLE_EDITOR, ROLE_USER, Principal, get_current, scopes_for_roles
from app.db.repository import get_session_factory
from app.db.schema import ApiKey, OAuthClient, OAuthToken
from app.mcp.protocol import MCP_PATH_RE
from app.org_profile import EXTERNAL_BASE_URL

log = structlog.get_logger(__name__)

router = APIRouter(tags=["admin"])

EXTERNAL_BASE = EXTERNAL_BASE_URL
EDGE_HEADER = "x-msp-edge"
KEY_PREFIX = "msp_"
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=60)
CODE_TTL = timedelta(minutes=10)
_ALLOWED_REDIRECTS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://vscode.dev/redirect",
}
_LOOPBACK_RE = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d{1,5})?(/[^\s]*)?$")
_FAILS: dict[str, deque[float]] = defaultdict(deque)
_FAIL_WINDOW_S = 600
_FAIL_LIMIT = 10


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """DB drivers differ on tz-awareness; treat naive values as UTC."""
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def _expired(dt: datetime | None) -> bool:
    return dt is None or _aware(dt) < _now()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def is_external(request: Request) -> bool:
    return request.headers.get(EDGE_HEADER) == "1"


def _client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


def _throttled(ip: str) -> bool:
    q = _FAILS[ip]
    cutoff = time.monotonic() - _FAIL_WINDOW_S
    while q and q[0] < cutoff:
        q.popleft()
    return len(q) >= _FAIL_LIMIT


def _record_fail(ip: str) -> None:
    _FAILS[ip].append(time.monotonic())


def redirect_allowed(uri: str) -> bool:
    return uri in _ALLOWED_REDIRECTS or bool(_LOOPBACK_RE.match(uri or ""))


def unauthorized_response() -> JSONResponse:
    return JSONResponse(
        {"error": "invalid_token", "error_description": "Connection key or OAuth token required"},
        status_code=401,
        headers={
            "WWW-Authenticate": f'Bearer resource_metadata="{EXTERNAL_BASE}/.well-known/oauth-protected-resource"'
        },
    )


def _key_usable(key: ApiKey | None) -> bool:
    return bool(key) and not key.revoked and (key.expires_at is None or not _expired(key.expires_at))


async def _key_by_secret(session: Any, secret: str) -> ApiKey | None:
    if not secret or not secret.startswith(KEY_PREFIX):
        return None
    key = (await session.execute(select(ApiKey).where(ApiKey.key_hash == _hash(secret)))).scalar_one_or_none()
    return key if _key_usable(key) else None


def _key_roles(key: ApiKey) -> frozenset[str]:
    return frozenset(key.roles or [ROLE_USER])


def _key_scopes(key: ApiKey) -> frozenset[str]:
    return frozenset(key.scopes or ["*:read"])  # keys from before scopes existed: read-only


MCP_RESOURCE_PATHS = MCP_PATH_RE  # /msp/mcp, /mcp (hub), /mcp/{connection}, /mcp/{connection}/{group}


def default_resource() -> str:
    return f"{EXTERNAL_BASE}/msp/mcp"


def resource_allowed(resource: str) -> bool:
    """An OAuth ``resource`` must be one of this server's MCP endpoints."""
    parsed = urlparse(resource or "")
    base = urlparse(EXTERNAL_BASE)
    return (parsed.scheme, parsed.netloc) == (base.scheme, base.netloc) \
        and bool(MCP_RESOURCE_PATHS.match(parsed.path.rstrip("/"))) and not parsed.query and not parsed.fragment


async def principal_from_token(request: Request) -> Principal | None:
    """Resolve a connection key or OAuth access token on the request, or None."""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()
    if not token:
        return None
    async with get_session_factory()() as session:
        key = await _key_by_secret(session, token)
        if key is not None:
            principal = Principal(id=f"key:{key.name}", kind="api_key", roles=_key_roles(key),
                                  scopes=_key_scopes(key), name=key.name)
        else:
            row = await session.get(OAuthToken, _hash(token))
            if not row or row.kind != "access" or _expired(row.expires_at):
                return None
            resource = row.resource or default_resource()
            if row.api_key_id.startswith("m365:"):
                roles = frozenset(row.user_roles or [])
                return Principal(id=row.user_upn or row.api_key_id, kind="oauth", roles=roles,
                                 scopes=scopes_for_roles(roles), name=row.user_upn or "", resource=resource)
            key = await session.get(ApiKey, row.api_key_id)
            if not _key_usable(key):
                return None
            principal = Principal(id=f"key:{key.name}", kind="oauth", roles=_key_roles(key),
                                  scopes=_key_scopes(key), name=key.name, resource=resource)
        if not key.last_used_at or (_now() - _aware(key.last_used_at)) > timedelta(minutes=5):
            key.last_used_at = _now()
            await session.commit()
        return principal


async def authenticate(request: Request) -> str | None:
    """Return who is behind the request's credential (``user@domain`` or ``key:<name>``), or None."""
    p = await principal_from_token(request)
    return p.id if p else None


# ── Discovery metadata ────────────────────────────────────────────────────


def _protected_resource(resource: str | None = None) -> dict[str, Any]:
    return {
        "resource": resource or default_resource(),
        "authorization_servers": [EXTERNAL_BASE],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
        "resource_name": "API Tools",
    }


@router.get("/.well-known/oauth-protected-resource")
async def protected_resource_root() -> dict[str, Any]:
    return _protected_resource()


@router.get("/.well-known/oauth-protected-resource/{path:path}")
async def protected_resource_mcp(path: str) -> dict[str, Any]:
    """Metadata of one MCP server (RFC 9728): each server is its own resource / token audience."""
    path = "/" + path.strip("/")
    if not MCP_RESOURCE_PATHS.match(path):
        raise HTTPException(404, "Not an MCP server on this host")
    return _protected_resource(f"{EXTERNAL_BASE}{path}")


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata() -> dict[str, Any]:
    return {
        "issuer": EXTERNAL_BASE,
        "authorization_endpoint": f"{EXTERNAL_BASE}/oauth/authorize",
        "token_endpoint": f"{EXTERNAL_BASE}/oauth/token",
        "registration_endpoint": f"{EXTERNAL_BASE}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    }


# ── Dynamic client registration ───────────────────────────────────────────


@router.post("/oauth/register", status_code=201)
async def register_client(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    uris = body.get("redirect_uris") or []
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and redirect_allowed(u) for u in uris):
        return JSONResponse(
            {"error": "invalid_redirect_uri", "error_description": "Only Claude / VS Code / loopback redirect URIs are accepted"},
            status_code=400,
        )
    async with get_session_factory()() as session:
        recent = (await session.execute(
            select(OAuthClient).where(OAuthClient.created_at > _now() - timedelta(hours=1))
        )).scalars().all()
    if len(recent) >= 50:
        log.warning("oauth.register_rate_limited", ip=_client_ip(request))
        return JSONResponse({"error": "temporarily_unavailable"}, status_code=429)
    client = OAuthClient(
        client_id="c_" + secrets.token_urlsafe(18),
        client_name=str(body.get("client_name") or "")[:255],
        redirect_uris=uris,
        created_at=_now(),
    )
    async with get_session_factory()() as session:
        session.add(client)
        await session.commit()
    log.info("oauth.client_registered", client_id=client.client_id, name=client.client_name)
    return JSONResponse(
        {
            "client_id": client.client_id,
            "client_id_issued_at": int(time.time()),
            "client_name": client.client_name,
            "redirect_uris": uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        status_code=201,
    )


# ── Authorization (sign-in page) ──────────────────────────────────────────

_AUTH_FIELDS = ("response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method", "state", "scope", "resource")


async def _validate_auth_request(p: dict[str, str]) -> tuple[OAuthClient | None, str | None]:
    if p.get("response_type") != "code":
        return None, "response_type must be code"
    if p.get("code_challenge_method") != "S256" or not p.get("code_challenge"):
        return None, "PKCE (S256) is required"
    if p.get("resource") and not resource_allowed(p["resource"]):
        return None, "resource is not an MCP server on this host"
    async with get_session_factory()() as session:
        client = await session.get(OAuthClient, p.get("client_id", ""))
    if not client:
        return None, "Unknown client"
    if p.get("redirect_uri") not in (client.redirect_uris or []) or not redirect_allowed(p.get("redirect_uri", "")):
        return None, "redirect_uri not registered for this client"
    return client, None


_PAGE_HEADERS = {
    "X-Frame-Options": "DENY",
    # no form-action: browsers apply it to the post-approval redirect back to Claude
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _auth_page(p: dict[str, str], client: OAuthClient, error: str = "") -> HTMLResponse:
    host = urlparse(p.get("redirect_uri", "")).hostname or "?"
    server = urlparse(p.get("resource") or default_resource()).path or "/"
    hidden = "".join(
        f'<input type="hidden" name="{f}" value="{html.escape(p.get(f, ""), quote=True)}">' for f in _AUTH_FIELDS
    )
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect to MSP Tools</title>
<style>
:root{{--bg:#f6f6f8;--card:#fff;--fg:#1d1d1f;--muted:#666;--line:#d9d9de;--accent:#2f6fde;--err:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151517;--card:#1e1e21;--fg:#ececf0;--muted:#a0a0a8;--line:#36363c;--accent:#6b9cff;--err:#ff8a80}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:48px 16px}}
.card{{background:var(--card);max-width:420px;margin:0 auto;padding:28px;border-radius:12px;border:1px solid var(--line)}}
h1{{font-size:20px;margin:0 0 8px}} p{{margin:8px 0}} .m{{color:var(--muted);font-size:14px}} .err{{color:var(--err)}}
input[type=password]{{width:100%;box-sizing:border-box;padding:10px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--fg);font:inherit;margin:12px 0}}
button{{width:100%;padding:10px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;cursor:pointer}}
</style></head><body><div class="card">
<h1>Connect to MSP Tools</h1>
<p class="m"><b>{html.escape(client.client_name or "An application")}</b> is asking to use the MCP server
<b>{html.escape(server)}</b> with your connection key's permissions. The access it gets works only for that server.
After you approve, you'll be sent back to <b>{html.escape(host)}</b>.</p>
{err}
<form method="post" action="/oauth/authorize">{hidden}
<label for="key">Connection key</label>
<input id="key" name="key" type="password" autocomplete="off" placeholder="msp_…" required autofocus>
<button type="submit">Approve</button></form>
<p class="m">Get a connection key from your MSP Tools administrator.</p>
</div></body></html>""", headers=_PAGE_HEADERS)


@router.get("/oauth/authorize", response_class=HTMLResponse)
async def authorize_get(request: Request):  # type: ignore[no-untyped-def]
    p = {k: v for k, v in request.query_params.items() if k in _AUTH_FIELDS}
    client, err = await _validate_auth_request(p)
    if err or client is None:
        return HTMLResponse(f"<p>Invalid authorization request: {html.escape(err or '')}</p>", status_code=400)
    from app.api.auth_m365 import entra_configured, start_flow

    if entra_configured():
        # Sign in with Microsoft 365 (Conditional Access/MFA) instead of a shared connection key.
        pending = {"client_id": client.client_id, "redirect_uri": p["redirect_uri"],
                   "code_challenge": p["code_challenge"], "state": p.get("state", ""),
                   "resource": p.get("resource") or default_resource()}
        return RedirectResponse(await start_flow("oauth", oauth_request=pending), status_code=302)
    return _auth_page(p, client)


@router.post("/oauth/authorize")
async def authorize_post(request: Request):  # type: ignore[no-untyped-def]
    from app.api.auth_m365 import entra_configured

    if entra_configured():
        return HTMLResponse("<p>Connection keys are not accepted here — sign in with single sign-on.</p>", 410)
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    p = {k: form.get(k, "") for k in _AUTH_FIELDS}
    client, err = await _validate_auth_request(p)
    if err or client is None:
        return HTMLResponse(f"<p>Invalid authorization request: {html.escape(err or '')}</p>", status_code=400)
    ip = _client_ip(request)
    if _throttled(ip):
        return _auth_page(p, client, "Too many attempts. Try again in a few minutes.")
    async with get_session_factory()() as session:
        key = await _key_by_secret(session, form.get("key", "").strip())
        if key is None:
            _record_fail(ip)
            log.warning("oauth.authorize_bad_key", ip=ip, client_id=client.client_id)
            return _auth_page(p, client, "That connection key isn't valid.")
        code = secrets.token_urlsafe(32)
        session.add(OAuthToken(
            token_hash=_hash(code), kind="code", client_id=client.client_id, api_key_id=key.id,
            redirect_uri=p["redirect_uri"], code_challenge=p["code_challenge"],
            expires_at=_now() + CODE_TTL, created_at=_now(), resource=p.get("resource") or default_resource(),
        ))
        await session.commit()
    log.info("oauth.authorized", client_id=client.client_id, api_key=key.prefix)
    params = {"code": code}
    if p.get("state"):
        params["state"] = p["state"]
    sep = "&" if "?" in p["redirect_uri"] else "?"
    return RedirectResponse(p["redirect_uri"] + sep + urlencode(params), status_code=302)


# ── Token endpoint ────────────────────────────────────────────────────────


def _token_error(error: str, desc: str = "") -> JSONResponse:
    return JSONResponse({"error": error, "error_description": desc}, status_code=400, headers={"Cache-Control": "no-store"})


M365_REFRESH_TTL = timedelta(days=14)  # M365 users re-sign-in at least every 2 weeks (picks up disabled accounts)


async def _issue_pair(session: Any, client_id: str, api_key_id: str, user_upn: str | None = None,
                      user_roles: list | None = None, resource: str | None = None) -> dict[str, Any]:
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(40)
    now = _now()
    refresh_ttl = M365_REFRESH_TTL if api_key_id.startswith("m365:") else REFRESH_TTL
    resource = resource or default_resource()
    session.add_all([
        OAuthToken(token_hash=_hash(access), kind="access", client_id=client_id, api_key_id=api_key_id,
                   expires_at=now + ACCESS_TTL, created_at=now, user_upn=user_upn, user_roles=user_roles,
                   resource=resource),
        OAuthToken(token_hash=_hash(refresh), kind="refresh", client_id=client_id, api_key_id=api_key_id,
                   expires_at=now + refresh_ttl, created_at=now, user_upn=user_upn, user_roles=user_roles,
                   resource=resource),
    ])
    # housekeeping: drop expired rows
    await session.execute(
        delete(OAuthToken).where(OAuthToken.expires_at < now - timedelta(days=1)),
        execution_options={"synchronize_session": False},
    )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TTL.total_seconds()),
        "refresh_token": refresh,
        "scope": "mcp",
    }


@router.post("/oauth/token")
async def token(request: Request) -> JSONResponse:
    raw = (await request.body()).decode()
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        import json as _json
        try:
            form = {k: str(v) for k, v in _json.loads(raw or "{}").items()}
        except ValueError:
            return _token_error("invalid_request")
    else:
        form = {k: v[0] for k, v in parse_qs(raw).items()}
    grant = form.get("grant_type")
    client_id = form.get("client_id", "")
    async with get_session_factory()() as session:
        if grant == "authorization_code":
            row = await session.get(OAuthToken, _hash(form.get("code", "")))
            if not row or row.kind != "code" or row.used or _expired(row.expires_at):
                return _token_error("invalid_grant", "code invalid or expired")
            if row.client_id != client_id or row.redirect_uri != form.get("redirect_uri"):
                return _token_error("invalid_grant", "client or redirect_uri mismatch")
            verifier = form.get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if not verifier or challenge != row.code_challenge:
                return _token_error("invalid_grant", "PKCE verification failed")
            row.used = True
        elif grant == "refresh_token":
            row = await session.get(OAuthToken, _hash(form.get("refresh_token", "")))
            if not row or row.kind != "refresh" or row.used or _expired(row.expires_at):
                return _token_error("invalid_grant", "refresh token invalid or expired")
            if client_id and row.client_id != client_id:
                return _token_error("invalid_grant", "client mismatch")
            row.used = True  # rotate
        else:
            return _token_error("unsupported_grant_type")
        requested = form.get("resource")
        if requested and requested != (row.resource or default_resource()):
            await session.commit()
            return _token_error("invalid_target", "resource does not match the authorization")
        if not row.api_key_id.startswith("m365:"):
            key = await session.get(ApiKey, row.api_key_id)
            if not _key_usable(key):
                await session.commit()
                return _token_error("invalid_grant", "connection key revoked or expired")
        out = await _issue_pair(session, row.client_id, row.api_key_id, row.user_upn, row.user_roles, row.resource)
        await session.commit()
    return JSONResponse(out, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


# ── Key management (LAN only) ─────────────────────────────────────────────


_SCOPE_RE = re.compile(r"^(\*|[a-z0-9_\-]{1,64}):(read|write)$")


class KeyCreate(BaseModel):
    name: str
    role: str = "user"  # user | editor — keys never get Approver/Admin (approvals need a person)
    scopes: list[str] = ["*:read"]
    expires_days: int | None = None


def _lan_only(request: Request) -> None:
    from app.security import is_internal_source

    if is_external(request) and not is_internal_source(request.headers.get("x-real-ip")):
        raise HTTPException(404)


async def _admin(request: Request) -> str:
    """Office/LAN only. The role check (Admin/Approver) is done by the middleware for /api/admin/*."""
    _lan_only(request)
    p = get_current()
    return p.id if p else "unknown"


@router.post("/api/admin/api-keys")
async def create_key(request: Request, body: KeyCreate) -> dict[str, Any]:
    """Create a connection key. The full key is shown ONCE — store it in a password manager."""
    actor = await _admin(request)
    if body.role not in ("user", "editor"):
        raise HTTPException(400, "role must be 'user' or 'editor'")
    scopes = sorted({s.strip().lower() for s in body.scopes if s.strip()})
    bad = [s for s in scopes if not _SCOPE_RE.match(s)]
    if bad or not scopes:
        raise HTTPException(400, f"Invalid scopes {bad or '[]'} — use '<platform|*>:<read|write>'")
    if body.role == "user" and any(s.endswith(":write") for s in scopes):
        raise HTTPException(400, "Write scopes need role 'editor'")
    if body.expires_days is not None and not 1 <= body.expires_days <= 730:
        raise HTTPException(400, "expires_days must be between 1 and 730")
    secret = KEY_PREFIX + secrets.token_urlsafe(32)
    roles = [ROLE_USER] + ([ROLE_EDITOR] if body.role == "editor" else [])
    key = ApiKey(id=str(uuid4()), name=body.name.strip()[:255] or "unnamed", prefix=secret[:10],
                 key_hash=_hash(secret), revoked=False, created_at=_now(), roles=roles, scopes=scopes,
                 expires_at=_now() + timedelta(days=body.expires_days) if body.expires_days else None,
                 created_by=actor)
    async with get_session_factory()() as session:
        session.add(key)
        await session.commit()
    log.info("api_key.created", name=key.name, prefix=key.prefix, roles=roles, scopes=scopes, by=actor)
    return {"id": key.id, "name": key.name, "key": secret, "roles": roles, "scopes": scopes,
            "expires_at": key.expires_at, "note": "Shown once. Store it securely."}


@router.get("/api/admin/api-keys")
async def list_keys(request: Request) -> list[dict[str, Any]]:
    _lan_only(request)
    async with get_session_factory()() as session:
        keys = (await session.execute(select(ApiKey).order_by(ApiKey.created_at))).scalars().all()
    return [
        {"id": k.id, "name": k.name, "prefix": k.prefix, "revoked": k.revoked,
         "roles": list(_key_roles(k)), "scopes": list(_key_scopes(k)), "expires_at": k.expires_at,
         "created_by": k.created_by, "created_at": k.created_at, "last_used_at": k.last_used_at}
        for k in keys
    ]


@router.get("/api/admin/edge-log")
async def edge_log(request: Request) -> list[dict[str, Any]]:
    """Recent requests that arrived through the internet edge, newest first (LAN only)."""
    _lan_only(request)
    from app.security import EDGE_LOG

    return list(reversed(EDGE_LOG))


@router.post("/api/admin/api-keys/{key_id}/revoke")
async def revoke_key(request: Request, key_id: str) -> dict[str, Any]:
    """Revoke a key and every OAuth token issued from it."""
    await _admin(request)
    async with get_session_factory()() as session:
        key = await session.get(ApiKey, key_id)
        if not key:
            raise HTTPException(404, "Key not found")
        key.revoked = True
        await session.execute(
            delete(OAuthToken).where(OAuthToken.api_key_id == key_id),
            execution_options={"synchronize_session": False},
        )
        await session.commit()
    log.info("api_key.revoked", name=key.name, prefix=key.prefix)
    return {"id": key_id, "revoked": True}


class RevokeUser(BaseModel):
    upn: str


@router.post("/api/admin/oauth/revoke-user")
async def revoke_user(request: Request, body: RevokeUser) -> dict[str, Any]:
    """Disconnect one person's Claude connector(s) immediately (all their OAuth tokens)."""
    await _admin(request)
    async with get_session_factory()() as session:
        res = await session.execute(
            delete(OAuthToken).where(OAuthToken.user_upn == body.upn.strip()),
            execution_options={"synchronize_session": False},
        )
        await session.commit()
    log.info("oauth.user_revoked", upn=body.upn, tokens=res.rowcount)
    return {"upn": body.upn, "tokens_revoked": res.rowcount or 0}


# ── Microsoft 365 sign-in for the Claude connector ────────────────────────


async def complete_m365_authorization(pending: dict[str, Any], *, upn: str, oid: str, roles: list[str]):  # type: ignore[no-untyped-def]
    """Called from /auth/callback: issue the OAuth code to Claude for this signed-in person."""
    async with get_session_factory()() as session:
        client = await session.get(OAuthClient, pending.get("client_id", ""))
        if not client or pending.get("redirect_uri") not in (client.redirect_uris or []) \
                or not redirect_allowed(pending.get("redirect_uri", "")):
            return HTMLResponse("<p>Invalid connector request — start Connect again from Claude.</p>", 400)
        code = secrets.token_urlsafe(32)
        session.add(OAuthToken(
            token_hash=_hash(code), kind="code", client_id=client.client_id, api_key_id=f"m365:{oid}",
            redirect_uri=pending["redirect_uri"], code_challenge=pending["code_challenge"],
            expires_at=_now() + CODE_TTL, created_at=_now(), user_upn=upn, user_roles=roles,
            resource=pending.get("resource") or default_resource(),
        ))
        await session.commit()
    log.info("oauth.authorized_m365", client_id=client.client_id, upn=upn, roles=roles)
    params = {"code": code}
    if pending.get("state"):
        params["state"] = pending["state"]
    sep = "&" if "?" in pending["redirect_uri"] else "?"
    return RedirectResponse(pending["redirect_uri"] + sep + urlencode(params), status_code=302)
