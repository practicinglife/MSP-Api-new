"""Get started → "Secure it": the permanent setup, before any API is connected (Admin only, /api/admin/…).

* Address and certificate — Let's Encrypt with a DNS record, or a certificate from any authority; or "only testing
  on this computer" (keeps the local certificate). Installing one rebuilds the app on the new address.
* Sign-in — Microsoft 365 (Entra ID), another OpenID Connect provider, or local accounts only.
* People — local accounts with a role each (password + authenticator).
* Who can do what — the three roles and exactly what each may call (``app.auth.principal.ACCESS``).

Choices are saved in ``data/settings.json`` (the sign-in secret encrypted); ``.env`` still wins where it sets them.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app import runtime
from app.auth.principal import (
    ACCESS,
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_EDITOR,
    ROLE_READER,
    WRITE_MODE_NOTE,
    get_current,
    top_role,
)
from app.db.repository import get_session_factory
from app.db.schema import LocalUser
from app.org_profile import external_base_url
from app.services import tls_setup

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/admin", tags=["setup"])

ROLE_CHOICES = {"reader": ROLE_READER, "editor": ROLE_EDITOR, "approver": ROLE_APPROVER, "admin": ROLE_ADMIN}
USERNAME_RE = re.compile(r"^[a-z0-9._-]{2,64}$")


def _who() -> str:
    p = get_current()
    return p.id if p else "unknown"


def _err(exc: Exception) -> HTTPException:
    return HTTPException(400, str(exc))


@router.get("/secure")
async def secure_status() -> dict[str, Any]:
    """Everything the "Secure it" step shows."""
    from app.api.auth_m365 import oidc_configured, provider_name

    settings = runtime.load_settings()
    sso = {k: v for k, v in (settings.get("sso") or {}).items() if k != "client_secret_enc"}
    async with get_session_factory()() as db:
        users = (await db.execute(select(LocalUser).order_by(LocalUser.username))).scalars().all()
    return {
        "address": external_base_url(),
        "address_from_env": runtime.set_by_operator("MSP_EXTERNAL_BASE_URL"),
        "can_restart": runtime.can_restart(),
        "tls": tls_setup.status(), "pending": tls_setup.pending_requests(),
        "letsencrypt_terms": tls_setup.TERMS_URL,
        "sso": {"active": oidc_configured(), "provider_name": provider_name() if oidc_configured() else "",
                "from_env": runtime.sso_from_env(), "saved": sso,
                "redirect_uri": f"{external_base_url()}/auth/callback",
                "app_roles": [ROLE_READER, ROLE_EDITOR, ROLE_APPROVER, ROLE_ADMIN]},
        "users": [{"username": u.username, "role": top_role(u.roles), "disabled": u.disabled,
                   "last_login_at": u.last_login_at} for u in users],
        "access": ACCESS, "write_mode_note": WRITE_MODE_NOTE,
        "secure_step": settings.get("secure_step", ""),
    }


# ── Certificate ──────────────────────────────────────────────────────────────────────────────────────────────


class AcmeStart(BaseModel):
    domain: str
    email: str = ""
    agree: bool = False
    staging: bool = False
    listen_on_network: bool = True


class Domain(BaseModel):
    domain: str


def _after_install(result: dict[str, Any]) -> dict[str, Any]:
    runtime.save_settings(secure_step="secure")
    restarting = runtime.request_restart()
    return {**result, "address": runtime.load_settings().get("external_url", ""),
            "restart": "automatic" if restarting else "manual"}


@router.post("/secure/tls/acme/start")
async def acme_start(body: AcmeStart) -> dict[str, Any]:
    try:
        return await tls_setup.acme_start(body.domain, body.email, agree=body.agree, staging=body.staging,
                                          listen_on_network=body.listen_on_network)
    except tls_setup.TlsError as exc:
        raise _err(exc) from None


@router.post("/secure/tls/acme/check")
async def acme_check(body: Domain) -> dict[str, Any]:
    try:
        return await tls_setup.dns_check(body.domain)
    except tls_setup.TlsError as exc:
        raise _err(exc) from None


@router.post("/secure/tls/acme/finish")
async def acme_finish(body: Domain) -> dict[str, Any]:
    try:
        result = await tls_setup.acme_finish(body.domain)
    except tls_setup.TlsError as exc:
        raise _err(exc) from None
    log.info("secure.certificate_installed", source="letsencrypt", domain=result["domain"], by=_who())
    return _after_install(result)


class Upload(BaseModel):
    cert_pem: str = ""
    key_pem: str = ""
    pfx_b64: str = ""
    pfx_password: str = ""
    domain: str = ""
    listen_on_network: bool = True


@router.post("/secure/tls/upload")
async def upload_certificate(body: Upload) -> dict[str, Any]:
    try:
        result = tls_setup.install_uploaded(cert_pem=body.cert_pem, key_pem=body.key_pem, pfx_b64=body.pfx_b64,
                                            pfx_password=body.pfx_password, domain=body.domain,
                                            listen_on_network=body.listen_on_network)
    except tls_setup.TlsError as exc:
        raise _err(exc) from None
    log.info("secure.certificate_installed", source="uploaded", domain=result["domain"], by=_who())
    return _after_install(result)


class StepChoice(BaseModel):
    mode: str  # "secure" (done) | "local" (only testing on this computer)


@router.post("/secure/step")
async def finish_step(body: StepChoice) -> dict[str, Any]:
    if body.mode not in ("secure", "local"):
        raise HTTPException(400, "mode is 'secure' or 'local'")
    runtime.save_settings(secure_step=body.mode)
    return {"secure_step": body.mode}


# ── Sign-in ──────────────────────────────────────────────────────────────────────────────────────────────────


class SsoBody(BaseModel):
    provider: str  # entra | oidc | none
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    issuer: str = ""
    display_name: str = ""
    roles_claim: str = ""
    role_map: dict[str, str] = {}


@router.post("/secure/sso")
async def save_sso(body: SsoBody) -> dict[str, Any]:
    """Save single sign-on (or turn it off). Takes effect immediately; local accounts keep working at /auth/local."""
    from app.auth.crypto import encrypt_value, master_key_configured
    from app.auth.principal import normalize_role

    if runtime.sso_from_env():
        raise HTTPException(409, "Single sign-on is set in .env on this server, which decides: change it there")
    old = (runtime.load_settings().get("sso") or {})
    if body.provider == "none":
        runtime.save_settings(sso={})
        runtime.apply_settings()
        return {"active": False}
    if body.provider not in ("entra", "oidc"):
        raise HTTPException(400, "provider is 'entra', 'oidc' or 'none'")
    secret_enc = old.get("client_secret_enc") if not body.client_secret and old.get("provider") == body.provider \
        else None
    if body.client_secret:
        if not master_key_configured():
            raise HTTPException(503, "Saving the client secret needs the encryption key (MSP_MASTER_KEY)")
        secret_enc = encrypt_value(body.client_secret)
    needed = {"entra": ["tenant_id", "client_id"], "oidc": ["issuer", "client_id"]}[body.provider]
    missing = [f for f in needed if not getattr(body, f).strip()] + ([] if secret_enc else ["client_secret"])
    if missing:
        raise HTTPException(400, f"Fill in: {', '.join(missing)}")
    if body.provider == "oidc":
        issuer = body.issuer.strip().rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{issuer}/.well-known/openid-configuration")
            r.raise_for_status()
            r.json()["authorization_endpoint"]
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"Couldn't read {issuer}/.well-known/openid-configuration ({exc})") from None
    bad = [v for v in body.role_map.values() if not normalize_role(v)]
    if bad:
        raise HTTPException(400, f"Map groups to Reader, Approver or Admin (not {', '.join(bad)})")
    sso = {"provider": body.provider, "client_id": body.client_id.strip(), "client_secret_enc": secret_enc}
    if body.provider == "entra":
        sso["tenant_id"] = body.tenant_id.strip()
    else:
        sso.update({"issuer": body.issuer.strip().rstrip("/"), "display_name": body.display_name.strip(),
                    "roles_claim": body.roles_claim.strip(), "role_map": body.role_map})
    runtime.save_settings(sso=sso)
    runtime.apply_settings()
    log.info("secure.sso_saved", provider=body.provider, by=_who())
    return {"active": True, "provider": body.provider, "redirect_uri": f"{external_base_url()}/auth/callback"}


# ── People (local accounts) ──────────────────────────────────────────────────────────────────────────────────


class NewUser(BaseModel):
    username: str
    password: str
    role: str = "reader"


@router.post("/users")
async def create_user(body: NewUser) -> dict[str, Any]:
    """A local account with a role. Returns the authenticator QR code once — give it to the person to scan."""
    from app.auth.passwords import hash_password, new_totp_secret, totp_qr_data_uri, totp_uri
    from app.org_profile import ORG_NAME

    username = body.username.strip().lower()
    role = ROLE_CHOICES.get(body.role.strip().lower())
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "User name: 2–64 characters, a-z, 0-9, . _ -")
    if role is None:
        raise HTTPException(400, "role is reader, editor, approver or admin")
    try:
        pw = hash_password(body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    secret = new_totp_secret()
    async with get_session_factory()() as db:
        if await db.get(LocalUser, username):
            raise HTTPException(409, f"{username} already exists")
        db.add(LocalUser(username=username, password_hash=pw, totp_secret=secret, roles=[role]))
        await db.commit()
    uri = totp_uri(secret, username, ORG_NAME)
    log.info("secure.user_created", username=username, role=role, by=_who())
    return {"username": username, "role": role, "totp_secret": secret, "totp_uri": uri,
            "totp_qr": totp_qr_data_uri(uri)}


class UserChange(BaseModel):
    role: str | None = None
    disabled: bool | None = None


@router.patch("/users/{username}")
async def change_user(username: str, body: UserChange) -> dict[str, Any]:
    def active_admins(users: list[LocalUser]) -> int:
        return sum(1 for u in users if not u.disabled and top_role(u.roles) == ROLE_ADMIN)

    async with get_session_factory()() as db:
        everyone = list((await db.execute(select(LocalUser))).scalars())
        before = active_admins(everyone)
        user = next((u for u in everyone if u.username == username.lower()), None)
        if user is None:
            raise HTTPException(404, "No such user")
        if body.role is not None:
            role = ROLE_CHOICES.get(body.role.strip().lower())
            if role is None:
                raise HTTPException(400, "role is reader, editor, approver or admin")
            user.roles = [role]
        if body.disabled is not None:
            user.disabled = body.disabled
        if before and not active_admins(everyone):
            raise HTTPException(409, "Keep at least one active local Admin (the break-glass login)")
        await db.commit()
        log.info("secure.user_changed", username=user.username, role=top_role(user.roles), disabled=user.disabled,
                 by=_who())
        return {"username": user.username, "role": top_role(user.roles), "disabled": user.disabled}
