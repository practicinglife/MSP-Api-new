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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from app.auth.passwords import (
    hash_password,
    new_totp_secret,
    totp_qr_data_uri,
    totp_uri,
    verify_totp,
)
from app.auth.principal import ROLE_ADMIN
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
        uri = totp_uri(secret, username, ORG_NAME)
        return JSONResponse({"step": "authenticator", "totp_secret": secret, "totp_uri": uri,
                             "totp_qr": totp_qr_data_uri(uri)})
    if not re.fullmatch(r"[A-Z2-7]{16,64}", body.totp_secret or "") or \
            verify_totp(body.totp_secret, body.totp_code) is None:
        return JSONResponse({"detail": "That authenticator code doesn't match — check the app and try again"}, 400)
    async with get_session_factory()() as db:
        if int((await db.execute(select(func.count()).select_from(LocalUser))).scalar() or 0):
            return JSONResponse({"detail": "Setup is already done — sign in instead"}, 409)
        db.add(LocalUser(username=username, password_hash=pw_hash, totp_secret=body.totp_secret,
                         roles=[ROLE_ADMIN]))
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
    from app.org_profile import EDGE_DOMAIN, external_base_url

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
                       "" if https else "Get started → Secure it (Let's Encrypt or your own certificate), or "
                                        "deploy/edge/setup-edge.sh / your own reverse proxy"))
    from app.services import tls_setup

    cert = tls_setup.status()
    trusted = cert.get("installed") and cert.get("source") not in ("local", "letsencrypt-staging")
    items.append(_item("certificate", "Certificate", True if trusted else (None if not cert.get("installed") else False),
                       (f"{cert['source']} certificate for {', '.join(cert.get('names', []))}, "
                        f"{cert.get('days_left')} days left" + (" — renew soon" if cert.get("renew_soon") else ""))
                       if cert.get("installed") else "No certificate installed by the app (a reverse proxy may have one).",
                       "" if trusted and not cert.get("renew_soon") else "Get started → Secure it (/setup?step=secure)"))
    external = bool(os.environ.get("MSP_EXTERNAL_BASE_URL", "").strip() or EDGE_DOMAIN)
    items.append(_item("external_url", "Address for AI clients and links", None if not external else True,
                       f"{external_base_url()} — used for OAuth, MCP addresses and links in change requests."
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
                       + "; ".join(f"{p['name']} — {p['description']}" for p in info["packs"])
                       + (" Set by MSP_PACKS in .env." if info["from_env"] else " Chosen in Get started."),
                       "Change in .env (MSP_PACKS), then restart" if info["from_env"] else
                       "Change it: Get started (/setup?step=choose)"))
    if "msp" in info["enabled"]:
        from app.services import company_matching

        async with get_session_factory()() as db:
            m = await company_matching.overview(db)
        c = m["counts"]
        pending = c["review"] + c["unmatched"]
        items.append(_item(
            "company_matching", "Company matching", None if not m["has_data"] else pending == 0,
            "Nothing synced yet: the first sync links company names across your tools and lists what needs an answer."
            if not m["has_data"] else
            f"{c['auto']} name(s) linked automatically, {c['manual']} by people; {pending} need an answer.",
            "Dashboard → Run Reconciliation" if not m["has_data"] else
            ("" if not pending else "Dashboard → Match companies (/#matching)")))
    default_mode = os.environ.get("MSP_WRITE_MODE_DEFAULT", "").strip() or "read_only"
    items.append(_item("write_mode", "Default write mode", None,
                       f"{default_mode}: new connections {'refuse writes' if default_mode == 'read_only' else 'allow writes per their mode'}"
                       " until an Approver changes them on the Connections page."))
    base = public_base_url(request)
    items.append(_item("connections", "Connections", n_conns > 0 if n_conns else None,
                       f"{n_conns} connection(s), {n_syncs} local cop{'y' if n_syncs == 1 else 'ies'}. "
                       f"MCP hub: {base}/mcp", "" if n_conns else "Add your first API on /connections"))
    return {"items": items, "needs_attention": sum(1 for i in items if i["ok"] is False)}


class PackChoice(BaseModel):
    packs: list[str] = []


@router.post("/api/admin/packs")
async def choose_packs(body: PackChoice) -> dict[str, Any]:
    """The Get started wizard's first question: MSP tools (the MSP pack) or any API (core only). Saved in
    data/settings.json; the app rebuilds itself when that changes which packs are on."""
    from app import packs, runtime

    if packs.from_env():
        raise HTTPException(409, "MSP_PACKS is set in .env, which decides the packs: change it there and restart")
    unknown = [p for p in body.packs if p not in packs.AVAILABLE]
    if unknown:
        raise HTTPException(400, f"Unknown pack(s): {', '.join(unknown)}")
    wanted = sorted(set(body.packs))
    runtime.save_settings(packs=wanted)
    if set(wanted) == packs.active():
        return {"packs": wanted, "restart": "none"}
    return {"packs": wanted, "restart": "automatic" if runtime.request_restart() else "manual"}


@router.get("/api/admin/getting-started")
async def getting_started() -> dict[str, Any]:
    """Where the Get started wizard is. First: which path (MSP tools or any API). MSP: tools connected → first
    sync → companies matched. Any API: first connection → tools → AI clients."""
    from app import packs
    from app.db.schema import Connection

    from app import runtime

    info = packs.describe()
    secure_step = runtime.load_settings().get("secure_step", "")
    base = {"chosen": info["chosen"], "from_env": info["from_env"], "secure_step": secure_step}
    if not secure_step:  # permanent sign-in and certificate come before connecting anything
        return {**base, "msp": "msp" in info["enabled"], "step": "secure"}
    if not info["chosen"]:
        return {**base, "msp": False, "step": "choose"}
    if "msp" not in info["enabled"]:
        async with get_session_factory()() as db:
            n = int((await db.execute(select(func.count()).select_from(Connection)
                                      .where(Connection.kind != "builtin"))).scalar() or 0)
        return {**base, "msp": False, "step": "connect" if not n else "done", "connections": n}
    from app.db.schema import PlatformDevice
    from app.services import builtin_integrations as builtins
    from app.services import company_matching

    tools = await builtins.catalog()
    connected = [t for t in tools if t["connected"]]
    async with get_session_factory()() as db:
        devices = int((await db.execute(select(func.count()).select_from(PlatformDevice))).scalar() or 0)
        pending = (await company_matching.by_tool(db))["pending"] if devices else 0
    step = "tools" if not connected else "sync" if not devices else "match" if pending else "done"
    return {**base, "msp": True, "step": step, "connected": [t["platform"] for t in connected],
            "management_connected": sum(t["role"] == "management" for t in connected),
            "security_connected": sum(t["role"] == "security" for t in connected),
            "devices": devices, "pending": pending}


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
main.wide{max-width:980px}
.steps{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0 4px;padding:0;list-style:none}
.steps li{font-size:13px;padding:4px 10px;border:1px solid var(--line);border-radius:99px;color:var(--muted)}
.steps li.on{border-color:var(--accent);color:var(--accent);font-weight:600}.steps li.done{color:var(--ok);border-color:var(--ok)}
.role{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin:18px 0 6px}
.tools{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px}
.tool{border:1px solid var(--line);border-radius:10px;padding:12px;background:var(--card)}
.tool summary{cursor:pointer;font-weight:600;list-style:none}.tool summary::-webkit-details-marker{display:none}
.pill{display:inline-block;font-size:12px;padding:0 8px;border-radius:99px;border:1px solid var(--line);margin-left:6px;font-weight:400}
.pill.ok{border-color:var(--ok)}.pill.warn{border-color:var(--warn)}
button.quiet{background:transparent;color:var(--fg);border-color:var(--line)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:8px 6px;border-top:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;border-top:0}
td input{padding:6px 8px}.chip{display:inline-block;font-size:12px;padding:1px 8px;margin:4px 4px 0 0;border:1px solid var(--line);border-radius:99px;cursor:pointer;background:var(--bg)}
.chip:hover{border-color:var(--accent)}.nm{display:flex;gap:6px;align-items:center;font-size:13px;color:var(--muted);white-space:nowrap}
.nm input{width:auto}
.spin{display:inline-block;width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:sp .7s linear infinite;vertical-align:-2px}
@keyframes sp{to{transform:rotate(360deg)}}
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
  if (!s.needs_setup) return home();
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
    <p class="m">The setup code is printed in the window where you started the app (<code>python -m app.cli run</code>),
      or in the server log with Docker (<code>docker compose logs app</code>); <code>python -m app.cli setup-code</code> shows it again.
      Next you'll scan a QR code with an authenticator app on your phone.</p>
    <label for="code">Setup code</label><input id="code" autocomplete="off" placeholder="XXXX-XXXX-XXXX">
    <label for="user">Username</label><input id="user" autocomplete="username" placeholder="admin">
    <label for="pw">Password (at least 12 characters)</label><input id="pw" type="password" autocomplete="new-password">
    <button id="next">Continue</button><div id="err1" class="bad"></div></div>
    <div class="card hidden" id="step2"><h2>Add an authenticator</h2>
    <p>Signing in needs the password <b>and</b> a 6-digit code from an authenticator app (Microsoft or Google Authenticator,
      1Password, …). Scan the QR code (or type the setup key), then enter the code it shows.</p>
    <img id="qr" alt="QR code for the authenticator app" width="240" height="240" style="background:#fff;border-radius:8px;display:block;margin:10px 0">
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
      secret = r.totp_secret; $("secret").textContent = secret; $("uri").href = r.totp_uri; $("qr").src = r.totp_qr;
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
    <p><a href="/connections">Go to Connections</a> · <a href="/setup?step=tools">Get started wizard</a> · <a href="/">Dashboard</a></p>`;
}

// ── Get started wizard: connect tools → first sync → match companies ─────────
const MSP_STEPS = [["login", "Your login"], ["secure", "Secure it"], ["choose", "What you're connecting"], ["tools", "Connect tools"], ["sync", "First sync"],
                   ["match", "Match companies"], ["done", "Done"]];
const CORE_STEPS = [["login", "Your login"], ["secure", "Secure it"], ["choose", "What you're connecting"], ["connect", "Connect your API"],
                    ["pick", "Make tools"], ["ai", "Add to your AI tools"], ["done", "Done"]];
const ROLE_TITLES = {management: "Management tools", security: "Security tools"};
const TOOL_LABELS = {connectwise_psa: "ConnectWise PSA", asio: "Asio", screenconnect: "ScreenConnect", sentinelone: "SentinelOne",
                     eset: "ESET", avast: "Avast", eset_connect: "ESET Connect"};
const title = (n) => String(n || "").split(" ").map(w => w ? w[0].toUpperCase() + w.slice(1) : w).join(" ");
const SELECT_STYLE = "width:100%;font:inherit;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)";
const AREA_STYLE = "width:100%;font:13px ui-monospace,Consolas,monospace;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)";
let gs = null;

async function home() {
  const r = await fetch("/api/admin/getting-started");
  if (r.status === 401) { location.href = "/auth/login?next=/setup"; return; }
  if (!r.ok) { $("sub").textContent = "Only administrators can set up this install."; return; }
  gs = await r.json();
  const want = new URLSearchParams(location.search).get("step");
  if (want === "checklist") return checklist();
  if (gs.step === "secure" && want !== "secure") return show("secure");
  if (gs.step === "choose") return show("choose");
  if (gs.step === "done" && !want) return checklist();
  show(want || gs.step);
}
function show(step) {
  document.querySelector("main").classList.add("wide");
  history.replaceState(null, "", "/setup?step=" + step);
  const steps = gs && gs.msp ? MSP_STEPS : CORE_STEPS;
  const at = steps.findIndex(s => s[0] === step);
  $("sub").innerHTML = `Get started <ol class="steps">${(step === "choose" || step === "secure" ? steps.slice(0, 3) : steps).map((s, i) =>
    `<li class="${i === at ? "on" : i < at ? "done" : ""}">${i < at ? "✓ " : ""}${esc(s[1])}</li>`).join("")}</ol>`;
  const pages = gs && gs.msp ? {secure: stepSecure, choose: stepChoose, tools: stepTools, sync: stepSync, match: stepMatch, done: stepDone}
                             : {secure: stepSecure, choose: stepChoose, connect: stepConnect, pick: stepPick, ai: stepAi, done: stepCoreDone};
  (pages[step] || pages[gs && gs.msp ? "tools" : "connect"])();
}

// Step: what are you connecting? MSP tools turn on the MSP pack and its wizard; any API gets the core wizard.
function stepChoose() {
  const card = (key, head, text, list) => `<button type="button" class="tool" data-choose="${key}"
      style="text-align:left;margin:0;color:var(--fg);background:var(--card);border-color:var(--line)">
      <div style="font-weight:600;font-size:16px">${head}</div><p class="m" style="margin:6px 0">${text}</p><div class="m">${list}</div></button>`;
  $("out").innerHTML = `<div class="card"><h2>What are you connecting?</h2>
    <p class="m">This sets the app up for how you'll use it. You can change it later from the install checklist.</p>
    <div class="tools" style="margin-top:12px">
      ${card("msp", "My MSP tools", "Ready-made integrations for running managed IT clients: one client list across your tools, antivirus coverage, tickets and reports.",
        "ConnectWise PSA · Asio · ScreenConnect · SentinelOne · ESET · Avast")}
      ${card("core", "Any API", "Connect any API or database. Every endpoint becomes a tool your AI assistants can use, behind your sign-in and approvals.",
        "OpenAPI · Postman · cURL · GraphQL · OData · SOAP · SQL · MongoDB · MCP")}
    </div>
    ${gs.from_env ? '<p class="m">Packs are set by <code>MSP_PACKS</code> in .env on this server, so that decides; this only picks the walkthrough.</p>' : ""}
    <div id="choose-status" class="m" style="margin-top:10px"></div></div>`;
  document.querySelectorAll("[data-choose]").forEach(b => b.onclick = async () => {
    const msp = b.dataset.choose === "msp";
    if (gs.from_env) {
      if (msp !== gs.msp) { $("choose-status").textContent = "Change MSP_PACKS in .env and restart to switch."; return; }
      return show(msp ? "tools" : "connect");
    }
    document.querySelectorAll("[data-choose]").forEach(x => x.disabled = true);
    $("choose-status").innerHTML = '<span class="spin"></span> Setting up…';
    try {
      const r = await api("/api/admin/packs", {method: "POST", body: JSON.stringify({packs: msp ? ["msp"] : []})});
      const next = "/setup?step=" + (msp ? "tools" : "connect");
      if (r.restart === "none") { gs = await (await fetch("/api/admin/getting-started")).json(); return show(msp ? "tools" : "connect"); }
      if (r.restart === "manual") { $("choose-status").innerHTML = '<span class="warn">Saved. Restart the app to apply it, then reload this page.</span>'; return; }
      $("choose-status").innerHTML = '<span class="spin"></span> Restarting the app with your choice…';
      await new Promise(res => setTimeout(res, 1500));
      for (let i = 0; i < 60; i++) {
        try { const h = await fetch("/health", {cache: "no-store"}); if (h.ok) { location.href = next; return; } } catch {}
        await new Promise(res => setTimeout(res, 1000));
      }
      $("choose-status").innerHTML = '<span class="bad">The app did not come back. Check the window it runs in, then reload.</span>';
    } catch (e) {
      $("choose-status").innerHTML = `<span class="bad">${esc(e.message)}</span>`;
      document.querySelectorAll("[data-choose]").forEach(x => x.disabled = false);
    }
  });
}

// ── Any API: connect → make tools → AI clients ─────────
const KIND_LABELS = {openapi: "OpenAPI / Swagger", postman: "Postman collection", curl: "cURL commands", soap: "SOAP (WSDL)",
                     graphql: "GraphQL", odata: "OData", sql: "SQL database", mongodb: "MongoDB", mcp: "Another MCP server"};
const AUTH_LABELS = {none: "None", api_key: "API key", basic: "Username and password", bearer: "Bearer token", headers: "Custom headers (JSON)",
                     oauth2_client_credentials: "OAuth 2 client credentials", wsse: "WS-Security (SOAP)"};
const FIELD_LABELS = {api_key: "API key", api_key_name: "Header or parameter name (e.g. X-API-Key)", api_key_in: "Sent in (header or query)",
                      username: "Username", password: "Password", token: "Token", headers: 'Headers as JSON, e.g. {"X-Key": "…"}',
                      client_id: "Client ID", client_secret: "Client secret", token_url: "Token URL", scope: "Scope (optional)",
                      audience: "Audience (optional)", client_auth: "Client auth (optional: body or basic)"};
const SECRET_FIELDS = new Set(["api_key", "password", "token", "client_secret"]);
let kinds = null, coreSlug = null;
try { coreSlug = sessionStorage.getItem("gs-slug"); } catch {}

let pickerLoaded = null;
function loadPicker() {
  return pickerLoaded || (pickerLoaded = new Promise((ok, fail) => {
    const sc = document.createElement("script"); sc.src = "/static/connection-picker.js"; sc.onload = ok; sc.onerror = fail;
    document.head.appendChild(sc);
  }));
}

async function stepConnect() {
  $("out").innerHTML = `<div class="card"><h2>Connect your API</h2>
    <p class="m">Pick a template and type only your key, search the public directory, or use Custom for anything else.
      Every endpoint becomes a tool. Credentials are stored encrypted.</p>
    <div id="gs-picker"></div><div id="gs-custom"></div>
    <div class="row"><a href="/setup?step=checklist" style="margin-top:14px">Skip for now</a></div></div>`;
  await loadPicker();
  ConnectionPicker.mount($("gs-picker"), {
    onCustom: () => customConnect($("gs-custom")),
    onLeaveCustom: () => { $("gs-custom").innerHTML = ""; },
    onSaved: (r) => {
      if (!r.slug) return;
      coreSlug = r.slug;
      try { sessionStorage.setItem("gs-slug", r.slug); } catch {}
      setTimeout(() => show("pick"), 900);
    },
  });
}

// The full form (any kind, any sign-in): the picker's Custom tab.
async function customConnect(box) {
  kinds = kinds || await api("/api/connections/kinds");
  box.innerHTML = `<div>
    <p class="m">Point at the API's definition. Scan reads it and lists every endpoint before anything is saved.</p>
    <label>Kind</label><select id="c-kind" style="${SELECT_STYLE}">
      ${kinds.kinds.map(k => `<option value="${esc(k)}">${esc(KIND_LABELS[k] || k)}</option>`).join("")}</select>
    <label>Name</label><input id="c-name" placeholder="e.g. Acme CRM">
    <label id="c-url-label">Definition URL</label><input id="c-url" placeholder="https://api.example.com/openapi.json">
    <label>…or paste the definition</label><textarea id="c-text" rows="5" style="${AREA_STYLE}"></textarea>
    <label>Base URL (optional when the definition has it)</label><input id="c-base" placeholder="https://api.example.com/v1">
    <label>Authentication</label><select id="c-auth" style="${SELECT_STYLE}">
      ${Object.keys(kinds.auth_types).map(a => `<option value="${esc(a)}">${esc(AUTH_LABELS[a] || a)}</option>`).join("")}</select>
    <div id="c-auth-fields"></div>
    <div class="row"><button type="button" id="c-scan">Scan</button><button type="button" id="c-save" class="quiet" disabled>Save and continue</button>
    </div>
    <div id="c-out" class="m" style="margin-top:10px"></div></div>`;
  const authFields = () => {
    const spec = kinds.auth_types[$("c-auth").value] || {secrets: [], settings: []};
    $("c-auth-fields").innerHTML = [...spec.secrets, ...spec.settings].map(f => `<label>${esc(FIELD_LABELS[f] || f)}</label>
      <input data-auth="${esc(f)}" type="${SECRET_FIELDS.has(f) ? "password" : "text"}" autocomplete="off">`).join("");
  };
  $("c-auth").onchange = authFields; authFields();
  $("c-kind").onchange = () => {
    const k = $("c-kind").value;
    $("c-url-label").textContent = {sql: "Connection string", mongodb: "Connection string (mongodb://…)", mcp: "MCP server URL",
                                    graphql: "GraphQL endpoint (or paste a schema)", odata: "Service URL"}[k] || "Definition URL";
  };
  const body = () => {
    const k = $("c-kind").value, url = $("c-url").value.trim(), text = $("c-text").value.trim(), auth = {type: $("c-auth").value};
    document.querySelectorAll("[data-auth]").forEach(i => {
      const v = i.value.trim();
      if (!v) return;
      if (i.dataset.auth === "headers") {
        try { auth.headers = JSON.parse(v); } catch { throw new Error("Headers must be JSON"); }
      } else auth[i.dataset.auth] = v;
    });
    const b = {kind: k, name: $("c-name").value.trim(), base_url: $("c-base").value.trim(), auth, source: {}};
    if (k === "sql" || k === "mongodb") { if (url) b.auth = {...auth, dsn: url}; if (text) b.source = {text}; }
    else if (k === "mcp" || ((k === "graphql" || k === "odata") && !text)) {
      if (url && !b.base_url) b.base_url = url;
      if (url && k !== "mcp") b.source = {url};
    }
    else b.source = text ? {text} : {url};
    return b;
  };
  $("c-scan").onclick = async () => {
    $("c-out").innerHTML = '<span class="spin"></span> Scanning…'; $("c-save").disabled = true;
    try {
      const r = await api("/api/connections?dry_run=true", {method: "POST", body: JSON.stringify(body())});
      if (!$("c-name").value.trim() && r.name) $("c-name").value = r.name;
      $("c-out").innerHTML = `<span class="ok">Found ${r.operations} endpoint${r.operations === 1 ? "" : "s"}</span>
        (${r.read_operations} read, ${r.write_operations} write) at ${esc(r.base_url || "no base URL yet")}
        ${r.warnings.length ? `<div class="warn">${r.warnings.map(esc).join("<br>")}</div>` : ""}
        <div style="margin-top:6px">${r.sample.slice(0, 12).map(o => `<div><code>${esc(o.method || "")}</code> ${esc(o.title || o.name)}</div>`).join("")}
        ${r.operations > 12 ? `<div>… and ${r.operations - 12} more</div>` : ""}</div>`;
      $("c-save").disabled = false; $("c-save").classList.remove("quiet");
    } catch (e) { $("c-out").innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
  };
  $("c-save").onclick = async () => {
    $("c-out").innerHTML = '<span class="spin"></span> Saving…';
    try {
      const r = await api("/api/connections", {method: "POST", body: JSON.stringify(body())});
      coreSlug = r.slug;
      try { sessionStorage.setItem("gs-slug", r.slug); } catch {}
      show("pick");
    } catch (e) { $("c-out").innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
  };
}

async function pickSlug() {
  const conns = (await api("/api/connections")).filter(c => c.kind !== "builtin");
  if (!conns.length) return null;
  if (!conns.some(c => c.slug === coreSlug)) coreSlug = conns[conns.length - 1].slug;
  return {conns, slug: coreSlug};
}

async function stepPick() {
  const p = await pickSlug();
  if (!p) return show("connect");
  const conn = p.conns.find(c => c.slug === p.slug);
  $("out").innerHTML = `<div class="card"><h2>Make tools from ${esc(conn.name)}</h2>
    <p class="m">AI assistants can already search and call all ${conn.operations} endpoints through this connection's MCP server.
      Tick the ones you'll use most to make each its own tool, so assistants see it right away. Try any of them here.</p>
    ${p.conns.length > 1 ? `<label>Connection</label><select id="p-conn" style="${SELECT_STYLE}">
      ${p.conns.map(c => `<option value="${esc(c.slug)}" ${c.slug === p.slug ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select>` : ""}
    <label>Find endpoints</label><input id="p-q" placeholder="e.g. list customers, create invoice" autocomplete="off">
    <div id="p-list" style="margin-top:10px"></div>
    <div class="row"><button type="button" id="p-next">Next: add to your AI tools</button></div></div>`;
  if ($("p-conn")) $("p-conn").onchange = e => {
    coreSlug = e.target.value;
    try { sessionStorage.setItem("gs-slug", coreSlug); } catch {}
    stepPick();
  };
  let timer = null;
  const base = `/api/connections/${encodeURIComponent(p.slug)}/operations`;
  const load = async () => {
    const q = $("p-q").value.trim();
    const res = await api(`${base}?limit=60${q ? "&q=" + encodeURIComponent(q) : ""}`);
    $("p-list").innerHTML = res.operations.length ? `<table><tr><th>Tool</th><th>Endpoint</th><th></th></tr>${res.operations.map((o, i) => `
      <tr><td><input type="checkbox" data-pin="${esc(o.name)}" ${o.pinned ? "checked" : ""} style="width:auto" title="Make this its own tool"></td>
        <td><code>${esc(o.method || "")}</code> <b>${esc(o.title || o.name)}</b>
          <span class="pill ${o.read_only ? "ok" : "warn"}">${o.read_only ? "read" : "write"}</span>
          <div class="m">${esc(o.name)}${o.description ? " · " + esc(o.description.slice(0, 140)) : ""}</div>
          <div id="try-${i}" class="hidden"><label>Arguments (JSON)</label><textarea rows="3" style="${AREA_STYLE}">{}</textarea>
            <button type="button" class="quiet" data-run="${esc(o.name)}" data-i="${i}">Run</button>
            <pre class="hidden" style="max-height:260px;overflow:auto"></pre></div></td>
        <td><button type="button" class="quiet" data-try="${i}" style="margin:0">Try</button></td></tr>`).join("")}</table>
      ${res.total > res.operations.length ? `<div class="m">Showing ${res.operations.length} of ${res.total}. Search to find others.</div>` : ""}`
      : '<div class="m">Nothing found. Try other words.</div>';
    document.querySelectorAll("[data-pin]").forEach(c => c.onchange = async () => {
      try {
        await api(`${base}/${encodeURIComponent(c.dataset.pin)}`, {method: "PATCH", body: JSON.stringify({pinned: c.checked})});
      } catch (e) { c.checked = !c.checked; alert(e.message); }
    });
    document.querySelectorAll("[data-try]").forEach(b => b.onclick = () => $("try-" + b.dataset.try).classList.toggle("hidden"));
    document.querySelectorAll("[data-run]").forEach(b => b.onclick = async () => {
      const box = $("try-" + b.dataset.i), out = box.querySelector("pre");
      out.classList.remove("hidden"); out.textContent = "Calling…";
      try {
        const args = JSON.parse(box.querySelector("textarea").value || "{}");
        const r = await api(`${base}/${encodeURIComponent(b.dataset.run)}/call`, {method: "POST", body: JSON.stringify({args})});
        out.textContent = JSON.stringify(r.result ?? r, null, 2).slice(0, 20000);
      } catch (e) { out.textContent = e.message; }
    });
  };
  $("p-q").oninput = () => { clearTimeout(timer); timer = setTimeout(load, 250); };
  $("p-next").onclick = () => show("ai");
  await load();
}

async function stepAi() {
  const p = await pickSlug();
  if (!p) return show("connect");
  const servers = await api("/api/mcp/servers");
  const server = servers.servers.find(s => s.connection === p.slug);
  const conn = p.conns.find(c => c.slug === p.slug);
  let key = "";
  const render = () => {
    const cfg = server ? server.configs : servers.hub.configs, url = server ? server.url : servers.hub.url;
    const fill = (t) => (typeof t === "string" ? t : JSON.stringify(t, null, 2)).split("<your connection key>").join(key || "<your connection key>");
    const items = [["Claude Code (terminal)", cfg.claude_code], ["VS Code: .vscode/mcp.json", cfg.vscode], ["Cursor: mcp.json", cfg.cursor],
                   ["Claude Desktop: claude_desktop_config.json (uses the bridge)", cfg.claude_desktop]];
    $("out").innerHTML = `<div class="card"><h2>Add ${esc(conn.name)} to your AI tools</h2>
      <p class="m">This connection is its own MCP server. Add it to Claude, VS Code, Cursor or any MCP client with a connection key.</p>
      <label>MCP server</label><pre>${esc(url)}</pre>
      <div class="m">All your connections in one server: <code>${esc(servers.hub.url)}</code></div>
      <h3 style="font-size:15px;margin:16px 0 6px">1. Connection key</h3>
      ${key ? `<pre>${esc(key)}</pre><div class="warn">Shown once. Save it in your password manager now. It's already filled in below.</div>`
            : `<p class="m">A key lets an AI client act for you, read-only to start. Create one here, or later under Connections.</p>
               <button type="button" id="ai-key">Create a connection key</button>`}
      <h3 style="font-size:15px;margin:16px 0 6px">2. Paste into your client</h3>
      ${items.map(([label, t], i) => `<label>${esc(label)}</label><pre id="cfg-${i}">${esc(fill(t))}</pre>
        <button type="button" class="quiet" data-copy="${i}" style="margin-top:4px">Copy</button>`).join("")}
      ${cfg.bridge_download ? `<p class="m">Claude Desktop needs <a href="${esc(cfg.bridge_download)}">mcp-bridge.py</a> (Python 3) in the folder you name.</p>` : ""}
      <div class="row"><button type="button" id="ai-done">Finish</button></div></div>`;
    document.querySelectorAll("[data-copy]").forEach(b => b.onclick = async () => {
      try { await navigator.clipboard.writeText($("cfg-" + b.dataset.copy).textContent); b.textContent = "Copied"; }
      catch { b.textContent = "Select and copy"; }
    });
    if ($("ai-key")) $("ai-key").onclick = async () => {
      try {
        const r = await api("/api/admin/api-keys", {method: "POST", body: JSON.stringify(
          {name: "AI tools (" + conn.name + ")", role: "user", scopes: ["*:read"], expires_days: 365})});
        key = r.key; render();
      } catch (e) { alert(e.message); }
    };
    $("ai-done").onclick = () => show("done");
  };
  render();
}

function stepCoreDone() {
  try { sessionStorage.removeItem("gs-slug"); } catch {}
  $("out").innerHTML = `<div class="card"><h2 class="ok">You're set up</h2>
    <p>Your API is connected and available to your AI tools. Writes stay off until you allow them: set a connection's write mode
      (read-only, direct, or approval) on the Connections page.</p>
    <div class="row"><a href="/connections"><button type="button">Open Connections</button></a>
      <a href="/setup?step=connect">Connect another API</a> · <a href="/setup?step=checklist">Install checklist</a></div></div>`;
}

// ── Secure it: certificate, sign-in, people, who can do what (before any API is connected) ─────────
const ROLE_LABEL = {reader: "Reader", editor: "Editor", approver: "Approver", admin: "Admin"};
let sec = null;

async function stepSecure() {
  sec = await api("/api/admin/secure");
  const t = sec.tls, s = sec.sso;
  const certLine = t.installed ? `${t.source === "local" ? "Local test certificate" : t.source === "uploaded" ? "Uploaded certificate" : "Let's Encrypt certificate"}
      for ${esc((t.names || []).join(", "))}, valid ${t.days_left} more days${t.issuer ? " (" + esc(t.issuer) + ")" : ""}` : "No certificate yet";
  const realCert = t.installed && t.source !== "local";
  $("out").innerHTML = `
    <div class="card"><h2>1. Address and certificate</h2>
      <p class="m">Serve this app on its permanent name with a trusted certificate, before connecting any API.
        Now: <b>${esc(sec.address)}</b> · ${certLine}${t.renew_soon ? ' <span class="warn">renew soon</span>' : ""}</p>
      ${sec.can_restart ? "" : '<p class="warn">This server runs under Docker or another web server: put the certificate on your reverse proxy (deploy/edge) and set MSP_EXTERNAL_BASE_URL. You can still mark this step done.</p>'}
      <div class="row" role="radiogroup">
        <label class="nm"><input type="radio" name="certmode" value="le" checked> Let's Encrypt (DNS record)</label>
        <label class="nm"><input type="radio" name="certmode" value="upload"> Upload a certificate</label>
        <label class="nm"><input type="radio" name="certmode" value="local"> Only testing on this computer</label>
      </div>
      <div id="cert-form"></div></div>
    <div class="card"><h2>2. Sign-in</h2>
      <p class="m">${s.active ? `Single sign-on is on: <b>${esc(s.provider_name)}</b>${s.from_env ? " (set in .env)" : ""}.` : "Now: local accounts (password + authenticator)."}
        Local accounts always keep working at /auth/local, as a break-glass login.</p>
      ${s.from_env ? `<p class="m">Single sign-on is configured in this server's .env, which decides. Change it there.</p>` : `
      <div class="row" role="radiogroup">
        <label class="nm"><input type="radio" name="ssomode" value="entra" ${s.saved.provider === "entra" ? "checked" : ""}> Microsoft 365</label>
        <label class="nm"><input type="radio" name="ssomode" value="oidc" ${s.saved.provider === "oidc" ? "checked" : ""}> Other OpenID Connect</label>
        <label class="nm"><input type="radio" name="ssomode" value="none" ${!s.saved.provider ? "checked" : ""}> Local accounts only</label>
      </div><div id="sso-form"></div>`}</div>
    <div class="card"><h2>3. People</h2>
      <p class="m">Local accounts, each with one role. With single sign-on, roles come from your identity provider instead.</p>
      <div id="people"></div></div>
    <div class="card"><h2>4. Who can do what</h2>
      <p class="m">Gated API access, by role. Each role includes everything above it. This is exactly what the app enforces.</p>
      ${accessTable()}<p class="m">${esc(sec.write_mode_note)}</p></div>
    <div class="row"><button type="button" id="sec-next">Continue: what are you connecting?</button>
      <span id="sec-status" class="m"></span></div>`;
  const certForms = {le: certLetsEncrypt, upload: certUpload, local: certLocal};
  document.querySelectorAll("[name=certmode]").forEach(r => r.onchange = () => certForms[r.value]());
  if (realCert) { document.querySelector("[name=certmode][value=upload]").checked = false; }
  certForms[sec.secure_step === "local" && !realCert ? "local" : "le"]();
  if (sec.secure_step === "local" && !realCert) document.querySelector("[name=certmode][value=local]").checked = true;
  if (!s.from_env) {
    const ssoForms = {entra: ssoEntra, oidc: ssoOidc, none: ssoNone};
    document.querySelectorAll("[name=ssomode]").forEach(r => r.onchange = () => ssoForms[r.value]());
    ssoForms[s.saved.provider || "none"]();
  }
  renderPeople();
  $("sec-next").onclick = () => {
    if (!realCert && sec.secure_step !== "local" && sec.can_restart) {
      $("sec-status").innerHTML = '<span class="warn">Install a certificate first, or choose "Only testing on this computer".</span>';
      return;
    }
    if (!sec.can_restart && !sec.secure_step) { api("/api/admin/secure/step", {method: "POST", body: JSON.stringify({mode: "secure"})}).then(next); return; }
    next();
  };
  async function next() { gs = await (await fetch("/api/admin/getting-started")).json(); show(gs.step === "secure" ? "choose" : (gs.step || "choose")); }
}

function accessTable() {
  const roles = ["Reader", "Editor", "Approver", "Admin"], rank = {Reader: 1, Editor: 2, Approver: 3, Admin: 4};
  return `<table><tr><th>Access</th>${roles.map(r => `<th>${r}</th>`).join("")}</tr>${sec.access.map(a =>
    `<tr><td>${esc(a.what)}</td>${roles.map(r => `<td>${rank[r] >= rank[a.role] ? '<span class="ok">✓</span>' : '<span class="m">–</span>'}</td>`).join("")}</tr>`).join("")}</table>`;
}

function copyable(label, value) {
  const id = "cp" + Math.random().toString(36).slice(2, 8);
  return `<label>${esc(label)}</label><div class="row"><pre id="${id}" style="flex:1;margin:0">${esc(value)}</pre>
    <button type="button" class="quiet" style="margin:0" data-copy="${id}">Copy</button></div>`;
}
function bindCopy(root) {
  root.querySelectorAll("[data-copy]").forEach(b => b.onclick = async () => {
    try { await navigator.clipboard.writeText($(b.dataset.copy).textContent); b.textContent = "Copied"; } catch { b.textContent = "Select and copy"; }
  });
}

async function waitForAddress(address, status) {
  status.innerHTML = `<span class="spin"></span> Restarting on ${esc(address)}…`;
  await new Promise(r => setTimeout(r, 2500));
  for (let i = 0; i < 40; i++) {
    try { const r = await fetch(address + "/health", {cache: "no-store", mode: "no-cors"}); if (r) { location.href = address + "/setup?step=secure"; return; } } catch {}
    await new Promise(r => setTimeout(r, 1500));
  }
  status.innerHTML = `<span class="warn">Restarted. Open <a href="${esc(address)}/setup?step=secure">${esc(address)}</a>: the name must point to this server (DNS or hosts file).</span>`;
}

function certLetsEncrypt() {
  const box = $("cert-form");
  box.innerHTML = `<p class="m">Works for internal servers too: you add one DNS record to prove you own the name. The name can point to an internal address.</p>
    <label>Server name</label><input id="le-domain" placeholder="tools.yourcompany.com">
    <label>Email for expiry notices (optional)</label><input id="le-email" placeholder="you@yourcompany.com">
    <label class="nm"><input type="checkbox" id="le-agree"> I accept the <a href="${esc(sec.letsencrypt_terms)}" target="_blank" rel="noopener">Let's Encrypt Subscriber Agreement</a></label>
    <label class="nm"><input type="checkbox" id="le-net" checked> Accept connections from other computers on the network</label>
    <label class="nm"><input type="checkbox" id="le-staging"> Practice run (Let's Encrypt staging: an untrusted test certificate)</label>
    <button type="button" id="le-start">Get the DNS record</button><div id="le-out" class="m" style="margin-top:10px"></div>`;
  $("le-start").onclick = async () => {
    const out = $("le-out"), domain = $("le-domain").value.trim();
    out.innerHTML = `<span class="spin"></span> Asking Let's Encrypt…`;
    try {
      const r = await api("/api/admin/secure/tls/acme/start", {method: "POST", body: JSON.stringify({
        domain, email: $("le-email").value.trim(), agree: $("le-agree").checked, staging: $("le-staging").checked, listen_on_network: $("le-net").checked})});
      out.innerHTML = `<p>Add this record at your DNS provider (GoDaddy, Cloudflare, Microsoft 365 DNS, your own DNS server…), then check it:</p>
        ${copyable("Type", r.record_type)}${copyable("Name", r.record_name)}${copyable("Value", r.record_value)}
        <div class="row"><button type="button" id="le-check">Check the record</button><button type="button" id="le-finish" class="quiet">Issue the certificate</button></div>
        <div id="le-msg" class="m" style="margin-top:8px"></div>`;
      bindCopy(out);
      $("le-check").onclick = async () => {
        try {
          const c = await api("/api/admin/secure/tls/acme/check", {method: "POST", body: JSON.stringify({domain})});
          $("le-msg").innerHTML = c.ready ? '<span class="ok">The record is visible. Issue the certificate.</span>'
            : `<span class="warn">Not visible yet${c.found.length ? " (found " + esc(c.found.join(", ")) + ")" : ""}. DNS changes can take a few minutes.</span>`;
          if (c.ready) $("le-finish").classList.remove("quiet");
        } catch (e) { $("le-msg").innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
      };
      $("le-finish").onclick = async () => {
        $("le-msg").innerHTML = `<span class="spin"></span> Let's Encrypt is checking the record…`;
        try {
          const done = await api("/api/admin/secure/tls/acme/finish", {method: "POST", body: JSON.stringify({domain})});
          $("le-msg").innerHTML = `<span class="ok">Certificate installed for ${esc(done.domain)} (valid ${done.days_left} days).</span>`;
          if (done.restart === "automatic") waitForAddress(done.address, $("le-msg"));
          else $("le-msg").innerHTML += " Restart the app to use it.";
        } catch (e) { $("le-msg").innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
      };
    } catch (e) { out.innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
  };
}

function certUpload() {
  const box = $("cert-form");
  box.innerHTML = `<p class="m">A certificate from your company's authority or a commercial one (DigiCert, GoDaddy, Sectigo …): PEM text, or a .pfx/.p12 file.</p>
    <label>Server name (optional: taken from the certificate)</label><input id="up-domain" placeholder="tools.yourcompany.com">
    <label>Certificate (PEM, with the chain after it)</label><textarea id="up-cert" rows="5" style="width:100%;font:12px ui-monospace,Consolas,monospace"></textarea>
    <label>Private key (PEM)</label><textarea id="up-key" rows="4" style="width:100%;font:12px ui-monospace,Consolas,monospace"></textarea>
    <label>…or a .pfx / .p12 file</label><input type="file" id="up-pfx" accept=".pfx,.p12"><label>.pfx password</label><input id="up-pass" type="password" autocomplete="off">
    <label class="nm"><input type="checkbox" id="up-net" checked> Accept connections from other computers on the network</label>
    <button type="button" id="up-go">Install the certificate</button><div id="up-out" class="m" style="margin-top:10px"></div>`;
  $("up-go").onclick = async () => {
    const out = $("up-out");
    let pfx_b64 = "";
    const f = $("up-pfx").files[0];
    if (f) { const buf = new Uint8Array(await f.arrayBuffer()); let bin = ""; buf.forEach(b => bin += String.fromCharCode(b)); pfx_b64 = btoa(bin); }
    out.innerHTML = '<span class="spin"></span> Checking…';
    try {
      const done = await api("/api/admin/secure/tls/upload", {method: "POST", body: JSON.stringify({
        cert_pem: $("up-cert").value, key_pem: $("up-key").value, pfx_b64, pfx_password: $("up-pass").value,
        domain: $("up-domain").value.trim(), listen_on_network: $("up-net").checked})});
      out.innerHTML = `<span class="ok">Installed for ${esc(done.domain)} (${esc(done.issuer)}, valid ${done.days_left} days).</span>`;
      if (done.restart === "automatic") waitForAddress(done.address, out); else out.innerHTML += " Restart the app to use it.";
    } catch (e) { out.innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
  };
}

function certLocal() {
  $("cert-form").innerHTML = `<p class="m">Keeps the local test certificate and this computer's address. Fine for trying things out; other computers can't
      use the app, and the install checklist keeps flagging it until a real certificate is installed.</p>
    <button type="button" id="lo-go" class="quiet">Only testing on this computer</button> <span id="lo-out" class="m"></span>`;
  $("lo-go").onclick = async () => {
    await api("/api/admin/secure/step", {method: "POST", body: JSON.stringify({mode: "local"})});
    sec.secure_step = "local"; $("lo-out").innerHTML = '<span class="ok">Noted. Continue below.</span>';
  };
}

function ssoEntra() {
  const s = sec.sso, saved = s.saved || {};
  $("sso-form").innerHTML = `<ol class="m" style="padding-left:18px">
      <li>Entra admin center → App registrations → New registration. Redirect URI (Web):</li></ol>
    ${copyable("Redirect URI", s.redirect_uri)}
    <ol class="m" start="2" style="padding-left:18px"><li>Certificates &amp; secrets → New client secret.</li>
      <li>App roles → create four, with these values: <b>Reader</b>, <b>Editor</b>, <b>Approver</b>, <b>Admin</b> (allowed member types: Users/Groups).</li>
      <li>Enterprise applications → this app → Users and groups → assign people or groups a role.</li></ol>
    <label>Directory (tenant) ID</label><input id="en-tenant" value="${esc(saved.tenant_id || "")}">
    <label>Application (client) ID</label><input id="en-client" value="${esc(saved.client_id || "")}">
    <label>Client secret</label><input id="en-secret" type="password" autocomplete="off" placeholder="${saved.provider === "entra" ? "saved, leave blank to keep" : ""}">
    <button type="button" id="en-save">Turn on Microsoft 365 sign-in</button><div id="en-out" class="m" style="margin-top:8px"></div>`;
  bindCopy($("sso-form"));
  $("en-save").onclick = () => saveSso({provider: "entra", tenant_id: $("en-tenant").value, client_id: $("en-client").value,
    client_secret: $("en-secret").value}, $("en-out"));
}

function ssoOidc() {
  const s = sec.sso, saved = s.saved || {};
  const map = Object.entries(saved.role_map || {});
  $("sso-form").innerHTML = `<p class="m">Google Workspace, Okta, Auth0, Keycloak, JumpCloud … Register a web app with this redirect URI:</p>
    ${copyable("Redirect URI", s.redirect_uri)}
    <label>Issuer URL</label><input id="oi-issuer" value="${esc(saved.issuer || "")}" placeholder="https://yourcompany.okta.com">
    <label>Client ID</label><input id="oi-client" value="${esc(saved.client_id || "")}">
    <label>Client secret</label><input id="oi-secret" type="password" autocomplete="off" placeholder="${saved.provider === "oidc" ? "saved, leave blank to keep" : ""}">
    <label>Button text (optional)</label><input id="oi-name" value="${esc(saved.display_name || "")}" placeholder="Okta">
    <label>Claim with roles or groups</label><input id="oi-claim" value="${esc(saved.roles_claim || "")}" placeholder="roles (or groups)">
    <label>Group → role (values already named Reader / Editor / Approver / Admin need no mapping)</label>
    <div id="oi-map">${(map.length ? map : [["", "reader"]]).map(([g, r]) => mapRow(g, r)).join("")}</div>
    <button type="button" class="quiet" id="oi-add">Add a group</button>
    <button type="button" id="oi-save">Turn on single sign-on</button><div id="oi-out" class="m" style="margin-top:8px"></div>`;
  bindCopy($("sso-form"));
  $("oi-add").onclick = () => $("oi-map").insertAdjacentHTML("beforeend", mapRow("", "reader"));
  $("oi-save").onclick = () => {
    const role_map = {};
    document.querySelectorAll(".maprow").forEach(row => {
      const g = row.querySelector("input").value.trim(); if (g) role_map[g] = ROLE_LABEL[row.querySelector("select").value];
    });
    saveSso({provider: "oidc", issuer: $("oi-issuer").value, client_id: $("oi-client").value, client_secret: $("oi-secret").value,
             display_name: $("oi-name").value, roles_claim: $("oi-claim").value, role_map}, $("oi-out"));
  };
}
function mapRow(group, role) {
  const r = String(role || "reader").toLowerCase();
  return `<div class="row maprow"><input style="flex:1" placeholder="group name or id" value="${esc(group)}">
    <select style="font:inherit;padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)">
    ${Object.entries(ROLE_LABEL).map(([k, l]) => `<option value="${k}" ${k === r ? "selected" : ""}>${l}</option>`).join("")}</select></div>`;
}
function ssoNone() {
  $("sso-form").innerHTML = `<p class="m">Everyone signs in with a local account (password + authenticator). Add people below.</p>
    ${sec.sso.saved.provider ? '<button type="button" id="no-save" class="quiet">Turn single sign-on off</button><div id="no-out" class="m"></div>' : ""}`;
  if ($("no-save")) $("no-save").onclick = () => saveSso({provider: "none"}, $("no-out"));
}
async function saveSso(body, out) {
  out.innerHTML = '<span class="spin"></span> Saving…';
  try {
    const r = await api("/api/admin/secure/sso", {method: "POST", body: JSON.stringify(body)});
    out.innerHTML = r.active ? `<span class="ok">On. Test it now: sign out and use the sign-in button (keep this tab open in case).</span>`
                             : '<span class="ok">Single sign-on is off.</span>';
    sec = await api("/api/admin/secure");
  } catch (e) { out.innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
}

function renderPeople() {
  const box = $("people");
  box.innerHTML = `<table><tr><th>User</th><th>Role</th><th></th></tr>${sec.users.map(u => `<tr>
      <td>${esc(u.username)}${u.disabled ? ' <span class="pill">disabled</span>' : ""}</td>
      <td><select data-urole="${esc(u.username)}" style="font:inherit;padding:6px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)">
        ${Object.entries(ROLE_LABEL).map(([k, l]) => `<option value="${k}" ${l === u.role ? "selected" : ""}>${l}</option>`).join("")}</select></td>
      <td><button type="button" class="quiet" style="margin:0" data-udis="${esc(u.username)}" data-v="${u.disabled ? "0" : "1"}">${u.disabled ? "Enable" : "Disable"}</button></td></tr>`).join("")}</table>
    <h3 style="font-size:15px;margin:14px 0 4px">Add a person</h3>
    <div class="row"><input id="nu-name" placeholder="user name" style="flex:1"><input id="nu-pw" type="password" placeholder="password (12+ characters)" style="flex:1" autocomplete="new-password">
      <select id="nu-role" style="font:inherit;padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)">
        ${Object.entries(ROLE_LABEL).map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select>
      <button type="button" id="nu-add" style="margin:0">Add</button></div><div id="nu-out" class="m" style="margin-top:8px"></div>`;
  box.querySelectorAll("[data-urole]").forEach(sel => sel.onchange = async () => {
    try { await api(`/api/admin/users/${encodeURIComponent(sel.dataset.urole)}`, {method: "PATCH", body: JSON.stringify({role: sel.value})}); }
    catch (e) { alert(e.message); }
    sec = await api("/api/admin/secure"); renderPeople();
  });
  box.querySelectorAll("[data-udis]").forEach(b => b.onclick = async () => {
    try { await api(`/api/admin/users/${encodeURIComponent(b.dataset.udis)}`, {method: "PATCH", body: JSON.stringify({disabled: b.dataset.v === "1"})}); }
    catch (e) { alert(e.message); }
    sec = await api("/api/admin/secure"); renderPeople();
  });
  $("nu-add").onclick = async () => {
    try {
      const r = await api("/api/admin/users", {method: "POST", body: JSON.stringify({username: $("nu-name").value, password: $("nu-pw").value, role: $("nu-role").value})});
      sec = await api("/api/admin/secure"); renderPeople();
      $("nu-out").innerHTML = `<p><b>${esc(r.username)}</b> (${esc(r.role)}) added. Give them their password and this code to scan with an authenticator app, once:</p>
        <img src="${r.totp_qr}" alt="Authenticator QR code" width="200" height="200" style="background:#fff;border-radius:8px">
        <div class="m">Setup key: <code>${esc(r.totp_secret)}</code></div>`;
    } catch (e) { $("nu-out").innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
  };
}

async function api(path, opts = {}) {
  const r = await fetch(path, {headers: {"Content-Type": "application/json"}, ...opts});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) || ("HTTP " + r.status));
  return data;
}

// Step: connect tools
async function stepTools() {
  const tools = await api("/api/connectors/catalog");
  const connected = tools.filter(t => t.connected);
  const card = (t) => `<details class="tool"><summary>${esc(t.label)}
      ${t.connected ? `<span class="pill ok">✓ connected${t.credentials_from === "env" ? " (.env)" : ""}</span>` : '<span class="pill">not connected</span>'}</summary>
      <div id="f-${esc(t.platform)}">${t.fields.filter(f => !f.show_when_auth).map(f => {
        const secret = f.type === "password", saved = t.values && t.values[f.key];
        return `<label>${esc(f.label)}${f.required ? "" : " (optional)"}</label>
          <input data-key="${esc(f.key)}" type="${secret ? "password" : "text"}" autocomplete="off"
            value="${esc(secret ? "" : (saved || f.default || ""))}" placeholder="${esc(secret && t.credentials_from === "app" ? "saved, leave blank to keep" : (f.default || ""))}">`;
      }).join("")}</div>
      <button type="button" data-connect="${esc(t.platform)}">${t.connected ? "Test and save" : "Connect"}</button>
      <div class="m" id="r-${esc(t.platform)}" style="margin-top:8px"></div></details>`;
  $("out").innerHTML = `<div class="card"><h2>Connect your tools</h2>
      <p class="m">Management tools hold your client list, and their company names usually agree. Security tools (antivirus / EDR) are matched to those
      clients in a later step. Each tool is checked by signing in and reading its company list. Credentials are stored encrypted on this server.</p>
      ${["management", "security"].map(role => `<div class="role">${ROLE_TITLES[role]}</div>
        <div class="tools">${tools.filter(t => t.role === role).map(card).join("")}</div>`).join("")}
      <div class="row" style="margin-top:16px">
        <button type="button" id="next" ${connected.length ? "" : "disabled"}>Next: first sync</button>
        <a href="/connections">Other APIs (Connections)</a> · <a href="/setup?step=checklist">Skip for now</a>
      </div><div class="m" id="hint">${connected.length ? `${connected.length} connected.` : "Connect at least one tool to continue."}</div></div>`;
  document.querySelectorAll("[data-connect]").forEach(b => b.onclick = async () => {
    const p = b.dataset.connect, out = $("r-" + p), creds = {};
    $("f-" + p).querySelectorAll("input[data-key]").forEach(i => { if (i.value.trim()) creds[i.dataset.key] = i.value.trim(); });
    b.disabled = true; out.innerHTML = '<span class="spin"></span> Signing in…';
    try {
      const res = await api("/api/connectors/register", {method: "POST", body: JSON.stringify({platform: p, credentials: creds, test_connection: true})});
      const t = res.test_result || {};
      if (res.saved) { out.innerHTML = `<span class="ok">Connected: ${t.companies_found ?? "?"} ${t.found_label || "companies"} found.</span>`; setTimeout(stepTools, 900); }
      else out.innerHTML = `<span class="bad">${esc(t.error || "Couldn't connect")}</span>`;
    } catch (e) { out.innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
    finally { b.disabled = false; }
  });
  $("next").onclick = () => show("sync");
}

// Step: first sync
function stepSync() {
  $("out").innerHTML = `<div class="card"><h2>First sync</h2>
    <p>Reads every company and device from your connected tools once and lines them up. This takes a minute or two.
      Afterwards it runs on its own, and only fetches what changed.</p>
    <button type="button" id="go">Start first sync</button> <span id="sync-status" class="m"></span></div>`;
  $("go").onclick = async () => {
    $("go").disabled = true;
    const t0 = Date.now(), tick = setInterval(() => {
      $("sync-status").innerHTML = `<span class="spin"></span> Syncing… ${Math.round((Date.now() - t0) / 1000)} s`; }, 500);
    try {
      const r = await api("/api/reconcile?mode=full", {method: "POST"});
      clearInterval(tick);
      $("sync-status").innerHTML = `<span class="ok">Done: ${r.company_matches ?? r.companies ?? "?"} companies, ${r.device_matches ?? r.devices ?? "?"} machines.</span>`;
      setTimeout(() => show("match"), 1200);
    } catch (e) { clearInterval(tick); $("sync-status").innerHTML = `<span class="bad">${esc(e.message)}</span>`; $("go").disabled = false; }
  };
}

// Step: match companies, tool by tool
async function stepMatch() {
  $("out").innerHTML = '<div class="card"><span class="spin"></span> Lining up company names…</div>';
  const d = await api("/api/companies/matching/by-tool");
  if (!d.has_data) { show("sync"); return; }
  const list = `<datalist id="clients">${d.clients.map(c => `<option value="${esc(c)}">${esc(title(c))}</option>`).join("")}</datalist>`;
  const rowHtml = (r, i) => {
    const ask = r.status === "needs_match";
    const value = ask ? (r.suggested || "") : (r.target || "");
    const noSuggestion = ask && !r.suggested && r.role === "management";
    const why = r.status === "same_name" ? "same name" : r.status === "linked" ? (r.source === "manual" ? "linked by a person" : (r.reason || "linked automatically"))
      : r.status === "not_managed" ? "not a managed client" : "";
    return `<tr data-i="${i}"><td><b>${esc(title(r.name))}</b><div class="m">${r.devices} device${r.devices === 1 ? "" : "s"}</div></td>
      <td><input list="clients" data-name="${esc(r.name)}" data-orig="${esc(value)}" data-ask="${ask ? 1 : 0}" value="${esc(value)}"
            placeholder="Type or pick the client" ${r.status === "not_managed" ? "disabled" : ""}>
        ${ask && r.candidates && r.candidates.length ? `<div>${r.candidates.slice(0, 4).map(c =>
          `<span class="chip" data-fill="${esc(c.name)}" title="${esc(c.reason || "")}">${esc(title(c.name))} · ${c.score}%</span>`).join("")}</div>` : ""}
        ${why ? `<div class="m">${esc(why)}</div>` : ""}</td>
      <td><label class="nm"><input type="checkbox" data-nm="${esc(r.name)}" ${r.status === "not_managed" || noSuggestion ? "checked" : ""}> Not a managed client</label></td></tr>`;
  };
  let i = 0;
  const sections = d.tools.filter(t => t.rows.length).map(t => {
    const ask = t.rows.filter(r => r.status === "needs_match"), settled = t.rows.filter(r => r.status !== "needs_match");
    t.rows.forEach(r => r.role = t.role);
    const head = `<tr><th>${esc(TOOL_LABELS[t.tool] || t.tool)} ${t.role === "security" ? "company / site" : "company"}</th><th>Client</th><th></th></tr>`;
    return `<div class="card"><h2>${esc(TOOL_LABELS[t.tool] || t.tool)} <span class="pill">${t.role}</span></h2>
      <div class="m">${t.role === "security" ? `${settled.length} matched to a client, ${ask.length} to check.`
        : `${ask.length} compan${ask.length === 1 ? "y is" : "ies are"} only in ${esc(TOOL_LABELS[t.tool] || t.tool)}: link each to a client, or leave it marked as not a managed client.`}</div>
      ${ask.length ? `<table>${head}${ask.map(r => rowHtml(r, i++)).join("")}</table>` : ""}
      ${settled.length ? `<details style="margin-top:10px"><summary class="m">Already matched (${settled.length}): check or change</summary>
        <table>${head}${settled.map(r => rowHtml(r, i++)).join("")}</table></details>` : ""}</div>`;
  }).join("");
  $("out").innerHTML = list + (sections || '<div class="card">Nothing to match: every company lines up.</div>') +
    `<div class="row"><button type="button" id="save">Save matches</button> <button type="button" class="quiet" id="later">Finish later</button>
     <span id="save-status" class="m"></span></div>`;
  document.querySelectorAll("[data-fill]").forEach(c => c.onclick = () => {
    const input = c.closest("td").querySelector("input[list]"); input.value = c.dataset.fill;
    const nm = c.closest("tr").querySelector("[data-nm]"); if (nm) nm.checked = false;
  });
  $("later").onclick = () => show("done");
  $("save").onclick = async () => {
    const links = [], not_managed = [];
    document.querySelectorAll("tr[data-i]").forEach(tr => {
      const input = tr.querySelector("input[list]"), nm = tr.querySelector("[data-nm]");
      if (nm && nm.checked) { if (!input.disabled) not_managed.push(nm.dataset.nm); return; }
      const v = input.value.trim();
      if (v && (input.dataset.ask === "1" || v !== input.dataset.orig) && v !== input.dataset.name) links.push({name: input.dataset.name, target: v});
    });
    $("save").disabled = true; $("save-status").innerHTML = '<span class="spin"></span> Saving…';
    try {
      const r = await api("/api/companies/matching/apply", {method: "POST", body: JSON.stringify({links, not_managed})});
      $("save-status").innerHTML = `<span class="ok">Saved ${r.saved}.</span>` + (r.errors.length ? ` <span class="bad">${esc(r.errors.join("; "))}</span>` : "");
      const again = await api("/api/companies/matching/by-tool");
      if (!again.pending) setTimeout(() => show("done"), 900); else setTimeout(stepMatch, 900);
    } catch (e) { $("save-status").innerHTML = `<span class="bad">${esc(e.message)}</span>`; $("save").disabled = false; }
  };
}

function stepDone() {
  $("out").innerHTML = `<div class="card"><h2 class="ok">You're set up</h2>
    <p>Your tools are connected, the first sync is done and companies are lined up. New companies are matched on every sync,
      and anything unclear shows up under <b>Match companies</b> on the dashboard.</p>
    <div class="row"><a href="/"><button type="button">Open the dashboard</button></a>
      <a href="/connections">Connections</a> · <a href="/setup?step=match">Matching again</a> · <a href="/setup?step=checklist">Install checklist</a></div></div>`;
}
start().catch(e => { $("sub").textContent = "Couldn't load: " + e.message; });
</script></body></html>"""
