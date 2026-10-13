"""Local accounts: sign-in without an identity provider (first run, break-glass, small installs).

Password (scrypt) **and** a 6-digit authenticator code (TOTP) are both required. Five wrong attempts lock the
account for 15 minutes; each code works once. Accounts are created with ``python -m app.cli create-user``.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.api.auth_m365 import _safe_next, create_session, oidc_configured, provider_name
from app.auth.passwords import DUMMY_HASH, verify_password, verify_totp
from app.db.repository import get_session_factory
from app.db.schema import LocalUser
from app.org_profile import ORG_NAME

log = structlog.get_logger(__name__)
router = APIRouter(tags=["admin"])

MAX_FAILS = 5
LOCKOUT = timedelta(minutes=15)

_PAGE_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def _page(next_url: str, error: str = "", status: int = 200) -> HTMLResponse:
    sso = (f'<p class="m"><a href="/auth/login?next={html.escape(next_url, quote=True)}">'
           f'Sign in with {html.escape(provider_name())}</a> instead.</p>') if oidc_configured() else ""
    err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · {html.escape(ORG_NAME)}</title>
<style>
:root{{--bg:#f6f6f8;--card:#fff;--fg:#1d1d1f;--muted:#666;--line:#d9d9de;--accent:#2f6fde;--err:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151517;--card:#1e1e21;--fg:#ececf0;--muted:#a0a0a8;--line:#36363c;--accent:#6b9cff;--err:#ff8a80}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:48px 16px}}
.card{{background:var(--card);max-width:380px;margin:0 auto;padding:28px;border-radius:12px;border:1px solid var(--line)}}
h1{{font-size:20px;margin:0 0 16px}} .m{{color:var(--muted);font-size:14px}} .err{{color:var(--err)}} a{{color:var(--accent)}}
label{{display:block;font-size:14px;margin-top:12px}}
input{{width:100%;box-sizing:border-box;padding:10px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--fg);font:inherit;margin-top:4px}}
button{{width:100%;margin-top:18px;padding:10px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;cursor:pointer}}
</style></head><body><div class="card">
<h1>Sign in to {html.escape(ORG_NAME)}</h1>{err}
<form method="post" action="/auth/local">
<input type="hidden" name="next" value="{html.escape(next_url, quote=True)}">
<label for="u">Username</label><input id="u" name="username" autocomplete="username" required autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<label for="c">Authenticator code</label><input id="c" name="code" inputmode="numeric" pattern="[0-9 ]{{6,7}}" autocomplete="one-time-code" required>
<button type="submit">Sign in</button></form>{sso}
</div></body></html>""", status_code=status, headers=_PAGE_HEADERS)


@router.get("/auth/local", response_class=HTMLResponse)
async def local_form(next: str | None = None) -> Response:  # noqa: A002
    from app.api.setup import needs_setup

    if await needs_setup():  # brand-new install: nobody to sign in as yet — create the first account
        return RedirectResponse("/setup", 302)
    return _page(_safe_next(next))


@router.post("/auth/local")
async def local_login(request: Request) -> Response:
    form: dict[str, Any] = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    username = str(form.get("username", "")).strip().lower()[:128]
    password, code = str(form.get("password", "")), str(form.get("code", ""))
    next_url = _safe_next(form.get("next"))
    fail = "Wrong username, password or code."

    async with get_session_factory()() as db:
        user = await db.get(LocalUser, username) if username else None
        if user is None or user.disabled:
            verify_password(password, DUMMY_HASH)  # same cost as a real check
            log.warning("local_auth.failed", username=username, reason="unknown_or_disabled")
            return _page(next_url, fail, 401)
        if user.locked_until and _aware(user.locked_until) > _now():
            log.warning("local_auth.locked", username=username)
            return _page(next_url, "Too many failed attempts. Try again later.", 429)

        step = verify_totp(user.totp_secret, code)
        ok = verify_password(password, user.password_hash) and step is not None and step > user.last_totp_step
        if not ok:
            user.failed_count += 1
            if user.failed_count >= MAX_FAILS:
                user.locked_until, user.failed_count = _now() + LOCKOUT, 0
            await db.commit()
            log.warning("local_auth.failed", username=username, reason="bad_credentials")
            return _page(next_url, fail, 401)

        user.failed_count, user.locked_until = 0, None
        user.last_totp_step, user.last_login_at = int(step or 0), _now()
        roles = list(user.roles or [])
        await db.commit()

    log.info("local_auth.signed_in", username=username, roles=roles)
    return await create_session(request, upn=f"local:{username}", oid=f"local:{username}", name=username,
                                roles=roles, next_url=next_url)
