"""Admin command line.

    python -m app.cli gen-master-key                   # value for MSP_MASTER_KEY in .env
    python -m app.cli create-user admin --role admin   # prompts for a password, prints the authenticator setup
    python -m app.cli reset-totp admin                 # new authenticator secret
    python -m app.cli set-password admin
    python -m app.cli disable-user admin | enable-user admin
    python -m app.cli list-users
    python -m app.cli create-key "claude.ai org" --role reader --scope "*:read" --expires-days 365
    python -m app.cli list-keys
    python -m app.cli revoke-key <id>                  # also kills every OAuth token issued from it
    python -m app.cli setup-code                       # code for the first-run /setup page
    python -m app.cli migrate                          # update the database schema (also runs at startup)
    python -m app.cli make-certs                       # local CA + HTTPS certificate for localhost (certs/)
    python -m app.cli run                              # start on this computer: https://localhost, no Docker needed
    python -m app.cli check-templates [id …]           # load every connection template's definition (live)

In Docker: ``docker compose exec app python -m app.cli create-user admin --role admin``.
Non-interactive password: set ``MSP_NEW_PASSWORD`` for the duration of the command.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from app.auth.crypto import generate_master_key
from app.auth.passwords import hash_password, new_totp_secret, totp_qr_terminal, totp_uri
from app.auth.principal import ROLE_ADMIN, ROLE_APPROVER, ROLE_EDITOR, ROLE_READER

# Each role includes the ones below it, so one is enough. Older names still work on the command line.
ROLE_ALIASES = {"reader": [ROLE_READER], "editor": [ROLE_EDITOR], "approver": [ROLE_APPROVER], "admin": [ROLE_ADMIN],
                "user": [ROLE_READER]}


def _password() -> str:
    pw = os.environ.get("MSP_NEW_PASSWORD")
    if pw:
        return pw
    first = getpass.getpass("New password (12+ characters): ")
    if first != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match")
    return first


def _print_totp(username: str, secret: str) -> None:
    from app.org_profile import ORG_NAME

    print("\nAdd this account to an authenticator app (Microsoft/Google Authenticator, 1Password, …):")
    print(f"  Secret key : {secret}")
    uri = totp_uri(secret, username, ORG_NAME)
    print(f"  Setup URI  : {uri}")
    totp_qr_terminal(uri)
    print("Sign-in needs the password AND the current 6-digit code. Store the secret like a password.\n")


async def _run(args: argparse.Namespace) -> None:
    from sqlalchemy import select

    from app.db.repository import get_session_factory, init_db
    from app.db.schema import LocalUser

    await init_db()
    factory = get_session_factory()
    username = (getattr(args, "username", "") or "").strip().lower()

    if args.cmd in ("create-key", "list-keys", "revoke-key"):
        await _keys(args, factory)
        return

    async with factory() as db:
        if args.cmd == "list-users":
            for u in (await db.execute(select(LocalUser).order_by(LocalUser.username))).scalars():
                state = "disabled" if u.disabled else "active"
                print(f"{u.username:24} {state:9} {', '.join(u.roles or [])}  last login: {u.last_login_at}")
            return
        user = await db.get(LocalUser, username)
        if args.cmd == "create-user":
            if user is not None:
                sys.exit(f"User {username} already exists")
            try:
                pw_hash = hash_password(_password())
            except ValueError as exc:
                sys.exit(str(exc))
            secret = new_totp_secret()
            db.add(LocalUser(username=username, password_hash=pw_hash, totp_secret=secret,
                             roles=ROLE_ALIASES[args.role]))
            await db.commit()
            print(f"Created {username} with role {args.role}.")
            _print_totp(username, secret)
            return
        if user is None:
            sys.exit(f"No user {username}")
        if args.cmd == "reset-totp":
            user.totp_secret, user.last_totp_step = new_totp_secret(), 0
            _print_totp(username, user.totp_secret)
        elif args.cmd == "set-password":
            try:
                user.password_hash = hash_password(_password())
            except ValueError as exc:
                sys.exit(str(exc))
            user.failed_count, user.locked_until = 0, None
            print("Password updated.")
        elif args.cmd in ("disable-user", "enable-user"):
            user.disabled = args.cmd == "disable-user"
            print(f"{username} {'disabled' if user.disabled else 'enabled'}.")
        await db.commit()


async def _keys(args: argparse.Namespace, factory) -> None:  # type: ignore[no-untyped-def]
    """Connection keys for programs/AI clients (same rules as POST /api/admin/api-keys)."""
    import hashlib
    import re
    import secrets
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from sqlalchemy import delete, select

    from app.db.schema import ApiKey, OAuthToken

    async with factory() as db:
        if args.cmd == "list-keys":
            for k in (await db.execute(select(ApiKey).order_by(ApiKey.created_at))).scalars():
                state = "revoked" if k.revoked else "active"
                print(f"{k.id}  {k.prefix}…  {state:7}  {k.name}  roles={k.roles or ['Reader']} "
                      f"scopes={k.scopes or ['*:read']} expires={k.expires_at} last_used={k.last_used_at}")
            return
        if args.cmd == "revoke-key":
            key = await db.get(ApiKey, args.key_id)
            if key is None:
                sys.exit(f"No key {args.key_id}")
            key.revoked = True
            await db.execute(delete(OAuthToken).where(OAuthToken.api_key_id == key.id),
                             execution_options={"synchronize_session": False})
            await db.commit()
            print(f"Revoked {key.name}.")
            return
        scopes = sorted({s.strip().lower() for s in args.scope or ["*:read"]})
        if any(not re.fullmatch(r"(\*|[a-z0-9_\-]{1,64}):(read|write)", s) for s in scopes):
            sys.exit("Scopes look like '*:read' or 'connectwise_psa:write'")
        role = {"user": "reader"}.get(args.role, args.role)
        if role == "reader" and any(s.endswith(":write") for s in scopes):
            sys.exit("Write scopes need --role editor")
        secret = "msp_" + secrets.token_urlsafe(32)
        roles = [ROLE_EDITOR if role == "editor" else ROLE_READER]
        db.add(ApiKey(id=str(uuid4()), name=args.name.strip()[:255], prefix=secret[:10],
                      key_hash=hashlib.sha256(secret.encode()).hexdigest(), revoked=False,
                      created_at=datetime.now(UTC), roles=roles, scopes=scopes, created_by="cli",
                      expires_at=datetime.now(UTC) + timedelta(days=args.expires_days) if args.expires_days else None))
        await db.commit()
        print(f"Key for '{args.name}' (roles {roles}, scopes {scopes}) — shown once, store it securely:\n\n  {secret}\n")


async def _check_templates(ids: list[str]) -> int:
    """Preview every template (dry run, placeholder credentials) and report how many operations its definition has.
    Templates that need real credentials just to read their definition (SQL, MongoDB, GraphQL, MCP) are skipped."""
    from app.connections.service import create_connection
    from app.services import connection_templates as ct

    failed = 0
    for t in ct.load():
        if ids and t["id"] not in ids:
            continue
        if t["kind"] in ("sql", "mongodb", "graphql", "mcp") or (t.get("auth") or {}).get("type") == "detect":
            print(f"  skip  {t['id']:18} ({t['kind']}: needs real credentials or a URL to read)")
            continue
        values = {f["key"]: f.get("default") or "example" for f in t.get("fields") or []}
        try:
            body = ct.build_body(t["id"], values)
            r = await create_connection({**body, "auth": {"type": "none"}}, "check-templates", dry_run=True)
            print(f"  ok    {t['id']:18} {r['operations']:5} operations ({r['read_operations']} read)"
                  + (f"  warnings: {'; '.join(r['warnings'])[:120]}" if r["warnings"] else ""))
            if not r["operations"]:
                failed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t['id']:18} {type(exc).__name__}: {str(exc)[:160]}")
    print(f"{failed} failed")
    return 1 if failed else 0


def _make_certs(args: argparse.Namespace) -> None:
    from pathlib import Path

    from app.tls import make_certs

    paths = make_certs(Path(args.dir), args.name)
    state = "Created" if paths.ca_created else "Kept existing"
    print(f"{state} CA:     {paths.ca_cert}  (keep {paths.ca_key} private)")
    print(f"Server certificate: {paths.server_cert}")
    print(f"Server key:         {paths.server_key}")
    if paths.ca_created:
        print("\nTrust the CA once so browsers accept it (Windows, current user; confirm the prompt):")
        print(f"    certutil -user -addstore Root {paths.ca_cert}")
    print("\nRun over HTTPS (set MSP_EXTERNAL_BASE_URL=https://localhost so sign-in redirects use it):")
    print(f"    uvicorn app.main:app --host 127.0.0.1 --port 443 "
          f"--ssl-certfile {paths.server_cert} --ssl-keyfile {paths.server_key}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="MSP API Tools admin commands")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-master-key", help="Print a new MSP_MASTER_KEY value")
    sub.add_parser("list-users", help="List local accounts")
    c = sub.add_parser("create-user", help="Create a local account (password + authenticator)")
    c.add_argument("username")
    c.add_argument("--role", choices=sorted(ROLE_ALIASES), default="reader",
                   help="reader, editor, approver or admin (each includes the ones before it)")
    for name in ("reset-totp", "set-password", "disable-user", "enable-user"):
        sub.add_parser(name).add_argument("username")
    k = sub.add_parser("create-key", help="Create a connection key for a program / AI client")
    k.add_argument("name")
    k.add_argument("--role", choices=["reader", "editor", "user"], default="reader",
                   help="reader or editor (keys are never approver or admin)")
    k.add_argument("--scope", action="append", help="'*:read' (default), 'connectwise_psa:write', … (repeatable)")
    k.add_argument("--expires-days", type=int, choices=range(1, 731), metavar="1-730")
    sub.add_parser("list-keys", help="List connection keys")
    sub.add_parser("revoke-key", help="Revoke a key and its OAuth tokens").add_argument("key_id")
    sub.add_parser("setup-code", help="Print the code the first-run /setup page asks for")
    sub.add_parser("migrate", help="Bring the database schema up to date (also runs at app startup)")
    r = sub.add_parser("run", help="Start the app on this computer (HTTPS, SQLite + generated key when not configured)")
    r.add_argument("--host", default=None,
                   help="Address to listen on (default 127.0.0.1, or the network once Get started set that up)")
    r.add_argument("--port", type=int, default=443, help="HTTPS port (default 443, falls back to 8443)")
    r.add_argument("--data", default="data", help="Folder for the SQLite database and generated key (default data/)")
    r.add_argument("--packs", default=None, help="Packs to turn on (default msp; 'none' for core only)")
    r.add_argument("--sqlite", action="store_true", help="Use the local SQLite database even if .env sets DATABASE_URL")
    r.add_argument("--no-sso", action="store_true", help="Local accounts only for this run (ignore MSP_ENTRA_* / MSP_OIDC_*)")
    ct = sub.add_parser("check-templates", help="Load each connection template's API definition (needs internet)")
    ct.add_argument("ids", nargs="*", help="Template ids (default: all)")
    m = sub.add_parser("make-certs", help="Create a local CA (once) and an HTTPS certificate for localhost")
    m.add_argument("--dir", default="certs", help="Where to write the files (default: certs/)")
    m.add_argument("--name", action="append", default=[], help="Extra hostname or IP to include (repeatable)")
    args = parser.parse_args(argv)
    if args.cmd == "run":
        from pathlib import Path

        from app.local_run import run

        run(host=args.host, port=args.port, data_dir=Path(args.data), packs=args.packs, sqlite=args.sqlite,
            no_sso=args.no_sso)
        return
    if args.cmd == "check-templates":
        sys.exit(asyncio.run(_check_templates(args.ids)))
    if args.cmd == "make-certs":
        _make_certs(args)
        return
    if args.cmd == "gen-master-key":
        print(generate_master_key())
        return
    if args.cmd == "setup-code":
        from app.api.setup import setup_code

        code = setup_code()
        if code is None:
            sys.exit("Set MSP_MASTER_KEY first (python -m app.cli gen-master-key)")
        print(code)
        return
    if args.cmd == "migrate":
        from app.db import migrations
        from app.db.repository import init_db

        asyncio.run(init_db())
        print(asyncio.run(migrations.status()))
        return
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
