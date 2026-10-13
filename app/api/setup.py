"""First-run setup and the install checklist.

* ``/setup`` — on a fresh install (no local accounts and no single sign-on) it creates the first admin:
  username, password and an authenticator app, confirmed with a code before the account exists. It needs the
  **setup code** printed in the server log at startup (also ``python -m app.cli setup-code``), so someone else
  on the network can't claim the install first. The code is derived from ``MSP_MASTER_KEY``; without a master
  key the page shows how to set one.
* After that, ``/setup`` shows admins the install checklist (``GET /api/admin/setup``): master key, sign-in,
  HTTPS / external URL, database and migrations, packs, default write mode, MCP addresses — each with what to
  put in ``.env``. Settings stay in ``.env``; the page never writes files.

The internet edge never reaches these pages (only MCP + OAuth paths pass it).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from app.auth.passwords import hash_password, new_totp_secret, totp_uri, verify_totp
from app.auth.principal import ROLE_ADMIN, ROLE_APPROVER, ROLE_EDITOR, ROLE_USER
from app.db.repository import get_session_factory
from app.db.schema import LocalUser

router = APIRouter(tags=["setup"])

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


def setup_code() -> str | None:
    """A short code derived from the master key, shown in the server log on first run (None without a key)."""
    raw = os.environ.get("MSP_MASTER_KEY", "").strip()
    if not raw:
        return None
    digest = hmac.new(raw.encode(), b"first-run-setup-v1", hashlib.sha256).digest()
    code = base64.b32encode(digest).decode()[:12]
    return f"{code[:4]}-{code[4:8]}-{code[8:]}"


async def needs_setup() -> bool:
    from app.api.auth_m365 import oidc_configured

    if oidc_configured():
        return False
    async with get_session_factory()() as db:
        return int((await db.execute(select(func.count()).select_from(LocalUser))).scalar() or 0) == 0


@router.get("/setup/status", include_in_schema=False)
async def status() -> dict[str, Any]:
    """Public: is this a fresh install? (Says nothing else once someone can sign in.)"""
    from app.auth.crypto import generate_master_key, master_key_configured

    if not await needs_setup():
        return {"needs_setup": False}
    out: dict[str, Any] = {"needs_setup": True, "master_key": master_key_configured()}
    if not out["master_key"]:
        out["suggested_master_key"] = generate_master_key()  # a fresh random value, not this server's
    return out


class FirstAdmin(BaseModel):
    setup_code: str
    username: str
    password: str
    totp_secret: str = ""
    totp_code: str = ""


@router.post("/setup/admin", include_in_schema=False)
async def create_first_admin(body: FirstAdmin) -> JSONResponse:
    """Step 1 (no ``totp_code``): check everything and return a new authenticator secret.
    Step 2 (with ``totp_secret`` + ``totp_code``): create the admin once the code proves the app is set up."""
    from app.auth import audit
    from app.org_profile import ORG_NAME

    if not await needs_setup():
        return JSONResponse({"detail": "Setup is already done — sign in instead"}, 409)
    expected = setup_code()
    if expected is None:
        return JSONResponse({"detail": "Set MSP_MASTER_KEY in .env and restart first"}, 409)
    given = body.setup_code.strip().upper().replace(" ", "")
    if not hmac.compare_digest(given.replace("-", ""), expected.replace("-", "")):
        return JSONResponse({"detail": "That setup code is wrong — it is in the server log"}, 403)
    username = body.username.strip().lower()
    if not USERNAME_RE.match(username):
        return JSONResponse({"detail": "Username: 2–64 characters, a-z, 0-9, . _ -"}, 400)
    try:
        pw_hash = hash_password(body.password)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, 400)

    if not body.totp_code:
        secret = new_totp_secret()
        return JSONResponse({"step": "authenticator", "totp_secret": secret,
                             "totp_uri": totp_uri(secret, username, ORG_NAME)})
    if not re.fullmatch(r"[A-Z2-7]{16,64}", body.totp_secret or "") or \
            verify_totp(body.totp_secret, body.totp_code) is None:
        return JSONResponse({"detail": "That authenticator code doesn't match — check the app and try again"}, 400)
    async with get_session_factory()() as db:
        if int((await db.execute(select(func.count()).select_from(LocalUser))).scalar() or 0):
            return JSONResponse({"detail": "Setup is already done — sign in instead"}, 409)
        db.add(LocalUser(username=username, password_hash=pw_hash, totp_secret=body.totp_secret,
                         roles=[ROLE_USER, ROLE_EDITOR, ROLE_APPROVER, ROLE_ADMIN]))
        await db.commit()
    await audit.record(principal=f"local:{username}", auth_kind="setup", source="ui", method="POST",
                       path="/setup/admin", status=200, action="first_admin_created")
    return JSONResponse({"step": "done", "username": username, "next": "/auth/local?next=/setup"})


# ── Checklist (admins) ────────────────────────────────────────────────────


def _item(key: str, title: str, ok: bool | None, detail: str, fix: str = "") -> dict[str, Any]:
    """ok: True = done, False = needs attention, None = optional / informational."""
    return {"key": key, "title": title, "ok": ok, "detail": detail, **({"fix": fix} if fix else {})}


@router.get("/api/admin/setup")
async def checklist(request: Request) -> dict[str, Any]:
    from app import packs
    from app.api.auth_m365 import oidc_configured, provider_name
    from app.auth.crypto import generate_master_key, master_key_configured
    from app.db import migrations
    from app.db.schema import Connection, ConnectionSync
    from app.mcp.protocol import public_base_url
    from app.org_profile import EDGE_DOMAIN, EXTERNAL_BASE_URL

    items = []
    items.append(_item("master_key", "Encryption key", master_key_configured(),
                       "Encrypts stored credentials (AES-256-GCM)." if master_key_configured() else
                       "Not set: credentials can't be saved.",
                       "" if master_key_configured() else f"MSP_MASTER_KEY={generate_master_key()}"))
    async with get_session_factory()() as db:
        admins = [u for u in (await db.execute(select(LocalUser))).scalars() if not u.disabled]
        n_conns = int((await db.execute(select(func.count()).select_from(Connection))).scalar() or 0)
        n_syncs = int((await db.execute(select(func.count()).select_from(ConnectionSync))).scalar() or 0)
    sso = oidc_configured()
    items.append(_item("sign_in", "Sign-in", sso or bool(admins),
                       (f"Single sign-on: {provider_name()}. " if sso else "No single sign-on. ")
                       + f"{len(admins)} local account(s) (password + authenticator).",
                       "" if sso else "Optional single sign-on: MSP_OIDC_ISSUER, MSP_OIDC_CLIENT_ID, "
                                      "MSP_OIDC_CLIENT_SECRET (or MSP_ENTRA_* for Microsoft 365)"))
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    items.append(_item("https", "HTTPS", https,
                       "This page was served over HTTPS." if https else
                       "This page came over plain HTTP: sign-in cookies and keys can be read on the network.",
                       "" if https else "Put the app behind TLS (deploy/edge/setup-edge.sh, or your own reverse "
                                        "proxy)"))
    external = bool(os.environ.get("MSP_EXTERNAL_BASE_URL", "").strip() or EDGE_DOMAIN)
    items.append(_item("external_url", "Address for AI clients and links", None if not external else True,
                       f"{EXTERNAL_BASE_URL} — used for OAuth, MCP addresses and links in change requests."
                       if external else "Not set: links use the address each request came in on (fine on a LAN).",
                       "" if external else "MSP_EXTERNAL_BASE_URL=https://tools.example.com"))
    from app.config import get_settings

    sqlite = get_settings().database_url.startswith("sqlite")
    mig = await migrations.status()
    items.append(_item("database", "Database", not sqlite and mig.get("up_to_date", False),
                       f"{'SQLite (fine for trying it out)' if sqlite else 'PostgreSQL'}; schema revision "
                       f"{mig.get('current') or 'none'} (latest {mig.get('head')}).",
                       "Use PostgreSQL in production (docker compose sets it up)" if sqlite else
                       ("" if mig.get("up_to_date") else "Restart the app: migrations run at startup")))
    info = packs.describe()
    items.append(_item("packs", "Packs", None,
                       "On: " + (", ".join(info["enabled"]) or "none (core only)") + ". Available: "
                       + "; ".join(f"{p['name']} — {p['description']}" for p in info["packs"]),
                       "MSP_PACKS=msp  (to turn on the MSP pack)" if not info["enabled"] else ""))
    default_mode = os.environ.get("MSP_WRITE_MODE_DEFAULT", "").strip() or "read_only"
    items.append(_item("write_mode", "Default write mode", None,
                       f"{default_mode}: new connections {'refuse writes' if default_mode == 'read_only' else 'allow writes per their mode'}"
                       " until an Approver changes them on the Connections page."))
    base = public_base_url(request)
    items.append(_item("connections", "Connections", n_conns > 0 if n_conns else None,
                       f"{n_conns} connection(s), {n_syncs} local cop{'y' if n_syncs == 1 else 'ies'}. "
                       f"MCP hub: {base}/mcp", "" if n_conns else "Add your first API on /connections"))
    return {"items": items, "needs_attention": sum(1 for i in items if i["ok"] is False)}


@router.get("/setup", response_class=HTMLResponse, include_in_schema=False)
async def setup_page() -> HTMLResponse:
    return HTMLResponse(_PAGE, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                        "Referrer-Policy": "no-referrer"})


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Set up API Tools</title>
<style>
:root{--bg:#f6f6f8;--card:#fff;--fg:#1d1d1f;--muted:#6a6a72;--line:#dedee3;--accent:#2f6fde;--accent-fg:#fff;
--ok:#1f7a3a;--warn:#9a6700;--bad:#b3261e;--code:#f2f2f5}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151517;--card:#1e1e21;--fg:#ececf0;
--muted:#a0a0a8;--line:#34343a;--accent:#6b9cff;--accent-fg:#0b1020;--ok:#5fd17f;--warn:#e3b341;--bad:#ff8a80;--code:#26262a}}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:24px 16px}
main{max-width:720px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 8px}
.m{color:var(--muted);font-size:13px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:14px 0}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
input{width:100%;font:inherit;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)}
button{margin-top:14px;padding:9px 16px;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:var(--accent-fg);font:inherit;cursor:pointer}
button:disabled{opacity:.6}
pre,code{background:var(--code);border-radius:6px;font:13px ui-monospace,Consolas,monospace}code{padding:1px 5px}
pre{padding:10px 12px;white-space:pre-wrap;word-break:break-all}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.hidden{display:none}
.item{display:grid;grid-template-columns:24px 1fr;gap:8px;padding:10px 0;border-top:1px solid var(--line)}.item:first-child{border-top:0}
a{color:var(--accent)}
</style></head><body><main>
<h1>Set up API Tools</h1><div class="m" id="sub">Checking this install…</div>
<div id="out"></div>
</main><script>
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
  return data;
}
async function start() {
  const s = await (await fetch("/setup/status")).json();
  if (!s.needs_setup) return checklist();
  if (!s.master_key) {
    $("sub").textContent = "First run — one thing to do on the server first.";
    $("out").innerHTML = `<div class="card"><h2>1. Set the encryption key</h2>
      <p>Add this line to the server's <code>.env</code> (it encrypts stored credentials, and the setup code comes from it), then restart the app:</p>
      <pre>MSP_MASTER_KEY=${esc(s.suggested_master_key)}</pre>
      <p class="m">Keep a copy somewhere safe: without it, saved credentials can't be decrypted. Then reload this page.</p></div>`;
    return;
  }
  $("sub").textContent = "First run: create the first administrator.";
  $("out").innerHTML = `<div class="card" id="step1"><h2>Create the first administrator</h2>
    <p class="m">The setup code is in the server log (look for <code>setup code</code>, e.g. <code>docker compose logs app</code>),
      or run <code>python -m app.cli setup-code</code> on the server.</p>
    <label for="code">Setup code</label><input id="code" autocomplete="off" placeholder="XXXX-XXXX-XXXX">
    <label for="user">Username</label><input id="user" autocomplete="username" placeholder="admin">
    <label for="pw">Password (at least 12 characters)</label><input id="pw" type="password" autocomplete="new-password">
    <button id="next">Continue</button><div id="err1" class="bad"></div></div>
    <div class="card hidden" id="step2"><h2>Add an authenticator</h2>
    <p>Signing in needs the password <b>and</b> a 6-digit code from an authenticator app (Microsoft or Google Authenticator,
      1Password, …). Add this account by its setup key or link, then enter the code it shows.</p>
    <label>Setup key</label><pre id="secret"></pre>
    <p class="m"><a id="uri" href="#">Open in an authenticator app</a> (on a phone)</p>
    <label for="totp">Code from the app</label><input id="totp" inputmode="numeric" autocomplete="one-time-code" maxlength="6">
    <button id="create">Create administrator</button><div id="err2" class="bad"></div></div>`;
  let secret = "";
  const base = () => ({setup_code: $("code").value, username: $("user").value, password: $("pw").value});
  $("next").onclick = async () => {
    $("err1").textContent = ""; $("next").disabled = true;
    try {
      const r = await post("/setup/admin", base());
      secret = r.totp_secret; $("secret").textContent = secret; $("uri").href = r.totp_uri;
      $("step2").classList.remove("hidden"); $("totp").focus();
    } catch (e) { $("err1").textContent = e.message; } finally { $("next").disabled = false; }
  };
  $("create").onclick = async () => {
    $("err2").textContent = ""; $("create").disabled = true;
    try {
      const r = await post("/setup/admin", {...base(), totp_secret: secret, totp_code: $("totp").value});
      $("out").innerHTML = `<div class="card"><h2 class="ok">Administrator ${esc(r.username)} created</h2>
        <p>Sign in with the password and a code from the app. The checklist for the rest of the setup comes next.</p>
        <a href="${esc(r.next)}">Sign in</a></div>`;
    } catch (e) { $("err2").textContent = e.message; $("create").disabled = false; }
  };
}
async function checklist() {
  const r = await fetch("/api/admin/setup");
  if (r.status === 401) { location.href = "/auth/login?next=/setup"; return; }
  if (!r.ok) { $("sub").textContent = "Only administrators can see the setup checklist."; return; }
  const c = await r.json();
  $("sub").textContent = c.needs_attention ? `${c.needs_attention} item(s) need attention. Settings live in the server's .env; restart after changing it.`
                                           : "Everything essential is in place.";
  $("out").innerHTML = `<div class="card">${c.items.map(i => `<div class="item">
      <div class="${i.ok === true ? "ok" : i.ok === false ? "bad" : "m"}">${i.ok === true ? "✓" : i.ok === false ? "!" : "•"}</div>
      <div><b>${esc(i.title)}</b><div class="m">${esc(i.detail)}</div>${i.fix ? `<pre>${esc(i.fix)}</pre>` : ""}</div></div>`).join("")}</div>
    <p><a href="/connections">Go to Connections</a></p>`;
}
start().catch(e => { $("sub").textContent = "Couldn't load: " + e.message; });
</script></body></html>"""
