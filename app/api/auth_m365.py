"""Single sign-on for people: any OpenID Connect provider (Microsoft Entra ID / Office 365 by default).

Authorization-code flow with PKCE. The ID token's **signature is verified against the provider's JWKS**
(issuer, audience, expiry, nonce, and the Entra tenant when configured) before anyone is signed in.

Configuration (.env), one of:
* Microsoft Entra ID: ``MSP_ENTRA_TENANT_ID``, ``MSP_ENTRA_CLIENT_ID``, ``MSP_ENTRA_CLIENT_SECRET``
* Any OIDC provider (Google, Okta, Keycloak, Auth0 …): ``MSP_OIDC_ISSUER``, ``MSP_OIDC_CLIENT_ID``,
  ``MSP_OIDC_CLIENT_SECRET``; optional ``MSP_OIDC_ROLES_CLAIM`` (default ``roles``; e.g. ``groups``),
  ``MSP_OIDC_ROLE_MAP`` (JSON: claim value → app role, e.g. ``{"it-admins": "Admin", "helpdesk": "Reader"}``),
  ``MSP_OIDC_DISPLAY_NAME`` (button text).

App roles: ``Reader`` (read, call read-only operations, propose), ``Editor`` (writes, submit changes),
``Approver`` (final approval, manage connections and credentials), ``Admin`` (everything, incl. users, keys,
sign-in, certificates). Older role values (``MSPTools.User`` / ``.Editor`` / ``.Approver`` / ``.Admin``) still work.
Without an IdP, people sign in with a local account (``/auth/local``, password + authenticator code).
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import jwt
import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import delete, select

from app.auth.principal import (  # noqa: F401
    ALL_ROLES,
    RANK,
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_EDITOR,
    ROLE_READER,
    ROLE_USER,
    normalize_role,
    normalize_roles,
    role_rank,
)
from app.db.repository import get_session_factory
from app.db.schema import AuthFlow, UserSession
from app.org_profile import external_base_url

log = structlog.get_logger(__name__)

router = APIRouter(tags=["admin"])

SESSION_COOKIE = "msp_session"
SESSION_TTL = timedelta(hours=8)
FLOW_TTL = timedelta(minutes=10)
_ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "ES256", "ES384"]
_CACHE_TTL_S = 3600
_cache: dict[str, tuple[float, Any]] = {}


# ── Provider configuration ────────────────────────────────────────────────


def _cfg() -> dict[str, str]:
    """Active OIDC provider settings, or {} when none is configured."""
    issuer = os.environ.get("MSP_OIDC_ISSUER", "").strip().rstrip("/")
    if issuer:
        cid, secret = os.environ.get("MSP_OIDC_CLIENT_ID", "").strip(), os.environ.get("MSP_OIDC_CLIENT_SECRET", "").strip()
        if cid and secret:
            return {"issuer": issuer, "client_id": cid, "client_secret": secret, "tenant": "",
                    "name": os.environ.get("MSP_OIDC_DISPLAY_NAME", "").strip() or "your organization"}
        return {}
    tenant = os.environ.get("MSP_ENTRA_TENANT_ID", "").strip()
    cid = os.environ.get("MSP_ENTRA_CLIENT_ID", "").strip()
    secret = os.environ.get("MSP_ENTRA_CLIENT_SECRET", "").strip()
    if tenant and cid and secret:
        return {"issuer": f"https://login.microsoftonline.com/{tenant}/v2.0", "client_id": cid,
                "client_secret": secret, "tenant": tenant,
                "name": os.environ.get("MSP_OIDC_DISPLAY_NAME", "").strip() or "Microsoft 365"}
    return {}


def oidc_configured() -> bool:
    return bool(_cfg())


entra_configured = oidc_configured  # older name, still imported elsewhere


def provider_name() -> str:
    return _cfg().get("name", "single sign-on")


async def _cached_json(url: str) -> dict[str, Any]:
    hit = _cache.get(url)
    if hit and hit[0] > time.monotonic():
        return hit[1]  # type: ignore[no-any-return]
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url)
    r.raise_for_status()
    data = r.json()
    _cache[url] = (time.monotonic() + _CACHE_TTL_S, data)
    return data  # type: ignore[no-any-return]


async def _discovery() -> dict[str, Any]:
    return await _cached_json(f"{_cfg()['issuer']}/.well-known/openid-configuration")


def _roles_from_claims(claims: dict[str, Any]) -> list[str]:
    claim = os.environ.get("MSP_OIDC_ROLES_CLAIM", "roles").strip() or "roles"
    raw = claims.get(claim) or []
    values = [raw] if isinstance(raw, str) else [str(v) for v in raw if isinstance(v, (str, int))]
    mapping: dict[str, str] = {}
    extra = os.environ.get("MSP_OIDC_ROLE_MAP", "").strip()
    if extra:
        try:
            mapping.update({str(k): str(v) for k, v in json.loads(extra).items()})
        except (ValueError, AttributeError):
            log.warning("oidc.role_map_invalid")
    # Mapped group/claim values first, then the value itself (Reader / Approver / Admin, or an older name)
    return sorted(normalize_roles(mapping.get(v, v) for v in values))


# ── Helpers ───────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def _hash(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def _redirect_uri() -> str:
    return f"{external_base_url()}/auth/callback"


def cookie_secure(request: Request) -> bool:
    """Secure cookies everywhere except plain-http local development."""
    return (request.url.hostname or "") not in ("localhost", "127.0.0.1", "testserver") \
        or request.url.scheme == "https"


# ── Sessions ──────────────────────────────────────────────────────────────


async def get_session(request: Request) -> UserSession | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    async with get_session_factory()() as db:
        s = await db.get(UserSession, _hash(raw))
    if not s or _aware(s.expires_at) < _now():
        return None
    return s


def has_role(session: UserSession | None, *roles: str) -> bool:
    if not session:
        return False
    wanted = [RANK[n] for n in (normalize_role(r) for r in roles) if n]
    return bool(wanted) and role_rank(session.roles) >= min(wanted)


async def require_role(request: Request, *roles: str) -> UserSession:
    """A signed-in *person* (browser session) with one of ``roles`` — used for submit/approve actions."""
    s = await get_session(request)
    if not s:
        raise HTTPException(401, "Sign in first: /auth/login")
    if not has_role(s, *roles):
        lowest = min((normalize_role(r) or r for r in roles), key=lambda r: RANK.get(r, 99))
        raise HTTPException(403, f"Requires the {lowest} role")
    return s


def check_csrf(request: Request, session: UserSession) -> None:
    token = request.headers.get("x-csrf-token", "")
    if not token or not secrets.compare_digest(token, session.csrf):
        raise HTTPException(403, "Missing or invalid CSRF token")


async def create_session(request: Request, *, upn: str, oid: str, name: str, roles: list[str],
                         next_url: str | None) -> Response:
    raw = secrets.token_urlsafe(32)
    async with get_session_factory()() as db:
        db.add(UserSession(id_hash=_hash(raw), upn=upn, oid=oid, name=name, roles=roles,
                           csrf=secrets.token_urlsafe(24), created_at=_now(), expires_at=_now() + SESSION_TTL))
        await db.execute(delete(UserSession).where(UserSession.expires_at < _now()),
                         execution_options={"synchronize_session": False})
        await db.commit()
    resp = RedirectResponse(_safe_next(next_url), 302)
    resp.set_cookie(SESSION_COOKIE, raw, max_age=int(SESSION_TTL.total_seconds()), httponly=True,
                    secure=cookie_secure(request), samesite="lax", path="/")
    return resp


# ── Sign-in flow ──────────────────────────────────────────────────────────


def _safe_next(url: str | None) -> str:
    """Only allow same-site relative paths as post-login destinations."""
    if not url or not url.startswith("/") or url.startswith("//") or url.startswith("/\\"):
        return "/"
    return url


async def start_flow(purpose: str, next_url: str | None = None, oauth_request: dict | None = None) -> str:
    """Create a sign-in flow and return the provider's authorize URL."""
    cfg = _cfg()
    disco = await _discovery()
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    async with get_session_factory()() as db:
        db.add(AuthFlow(state=state, nonce=nonce, code_verifier=verifier, purpose=purpose,
                        next_url=_safe_next(next_url) if purpose == "session" else None,
                        oauth_request=oauth_request, expires_at=_now() + FLOW_TTL))
        await db.execute(delete(AuthFlow).where(AuthFlow.expires_at < _now()),
                         execution_options={"synchronize_session": False})
        await db.commit()
    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": _redirect_uri(),
        "response_mode": "query",
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return f"{disco['authorization_endpoint']}?{urlencode(params)}"


async def _exchange(code: str, verifier: str) -> dict[str, Any]:
    cfg = _cfg()
    disco = await _discovery()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            disco["token_endpoint"],
            data={
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(),
                "code_verifier": verifier,
                "scope": "openid profile email",
            },
        )
    if r.status_code != 200:
        log.warning("oidc.token_exchange_failed", status=r.status_code, body=r.text[:300])
        raise HTTPException(400, "Sign-in failed at the identity provider")
    return r.json()  # type: ignore[no-any-return]


async def _signing_key(id_token: str) -> Any:
    kid = jwt.get_unverified_header(id_token).get("kid")
    disco = await _discovery()
    for attempt in range(2):
        jwks = await _cached_json(disco["jwks_uri"])
        for jwk in jwks.get("keys", []):
            if kid is None or jwk.get("kid") == kid:
                return jwt.PyJWK(jwk).key
        if attempt == 0:  # provider rotated keys → refetch once
            _cache.pop(disco["jwks_uri"], None)
    raise HTTPException(400, "Sign-in could not be verified (unknown signing key)")


async def verify_id_token(id_token: str, nonce: str) -> dict[str, Any]:
    """Verify signature, issuer, audience, expiry and nonce; return the claims."""
    cfg = _cfg()
    disco = await _discovery()
    try:
        claims: dict[str, Any] = jwt.decode(
            id_token, key=await _signing_key(id_token), algorithms=_ALGORITHMS,
            audience=cfg["client_id"], issuer=disco.get("issuer", cfg["issuer"]), leeway=60,
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        log.warning("oidc.id_token_invalid", error=str(exc))
        raise HTTPException(400, "Sign-in could not be verified") from exc
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        log.warning("oidc.id_token_invalid", error="nonce")
        raise HTTPException(400, "Sign-in could not be verified")
    if cfg.get("tenant") and claims.get("tid") != cfg["tenant"]:
        log.warning("oidc.id_token_invalid", error="tid")
        raise HTTPException(400, "Sign-in could not be verified")
    return claims


@router.get("/auth/login")
async def login(request: Request, next: str | None = None):  # type: ignore[no-untyped-def]  # noqa: A002
    if not oidc_configured():
        return RedirectResponse(f"/auth/local?{urlencode({'next': _safe_next(next)})}", 302)
    host = (request.url.hostname or "").lower()
    if host != urlparse(external_base_url()).hostname:
        # Cookies + the IdP redirect live on the configured public URL.
        return RedirectResponse(f"{external_base_url()}/auth/login?{urlencode({'next': _safe_next(next)})}", 302)
    return RedirectResponse(await start_flow("session", next_url=next), 302)


@router.get("/auth/callback")
async def callback(request: Request):  # type: ignore[no-untyped-def]
    if not oidc_configured():
        raise HTTPException(503, "Single sign-on is not configured")
    q = request.query_params
    if q.get("error"):
        return HTMLResponse(f"<p>Sign-in was not completed: "
                            f"{html.escape(q.get('error_description', q['error'])[:300])}</p>", 400)
    async with get_session_factory()() as db:
        flow = await db.get(AuthFlow, q.get("state", ""))
        if flow:
            await db.delete(flow)  # single use
            await db.commit()
    if not flow or _aware(flow.expires_at) < _now():
        return HTMLResponse("<p>Sign-in request expired. Start again.</p>", 400)

    tokens = await _exchange(q.get("code", ""), flow.code_verifier)
    claims = await verify_id_token(tokens.get("id_token", ""), flow.nonce)
    roles = _roles_from_claims(claims)
    upn = claims.get("preferred_username") or claims.get("email") or claims.get("upn") or claims.get("sub", "")
    oid = claims.get("oid") or claims.get("sub", "")
    name = claims.get("name", upn)
    log.info("oidc.signed_in", upn=upn, roles=roles, purpose=flow.purpose)

    if not roles:
        return HTMLResponse(
            f"<p>{html.escape(upn)} is signed in but has no role here (Reader, Editor, Approver or Admin). "
            "Ask your administrator to assign one.</p>", 403
        )

    if flow.purpose == "oauth":
        from app.api.external_auth import complete_m365_authorization

        return await complete_m365_authorization(flow.oauth_request or {}, upn=upn, oid=oid, roles=roles)

    return await create_session(request, upn=upn, oid=oid, name=name, roles=roles, next_url=flow.next_url)


@router.post("/auth/logout")
async def logout(request: Request) -> JSONResponse:
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        async with get_session_factory()() as db:
            await db.execute(delete(UserSession).where(UserSession.id_hash == _hash(raw)),
                             execution_options={"synchronize_session": False})
            await db.commit()
    resp = JSONResponse({"signed_out": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/auth/me")
async def me(request: Request) -> dict[str, Any]:
    s = await get_session(request)
    if not s:
        return {"signed_in": False, "configured": oidc_configured(), "provider": provider_name()}
    roles = sorted(normalize_roles(s.roles), key=lambda r: RANK[r])
    return {"signed_in": True, "upn": s.upn, "name": s.name, "roles": roles,
            "role": roles[-1] if roles else "", "csrf": s.csrf, "expires_at": s.expires_at}


async def sessions_for_upn(upn: str) -> list[UserSession]:
    async with get_session_factory()() as db:
        return list((await db.execute(select(UserSession).where(UserSession.upn == upn))).scalars())
