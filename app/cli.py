"""Admin command line.

    python -m app.cli gen-master-key                   # value for MSP_MASTER_KEY in .env
    python -m app.cli create-user admin --role admin   # prompts for a password, prints the authenticator setup
    python -m app.cli reset-totp admin                 # new authenticator secret
    python -m app.cli set-password admin
    python -m app.cli disable-user admin | enable-user admin
    python -m app.cli list-users
    python -m app.cli create-key "claude.ai org" --role user --scope "*:read" --expires-days 365
    python -m app.cli list-keys
    python -m app.cli revoke-key <id>                  # also kills every OAuth token issued from it
    python -m app.cli setup-code                       # code for the first-run /setup page
    python -m app.cli migrate                          # update the database schema (also runs at startup)

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
from app.auth.passwords import hash_password, new_totp_secret, totp_uri
from app.auth.principal import ROLE_ADMIN, ROLE_APPROVER, ROLE_EDITOR, ROLE_USER

ROLE_ALIASES = {"user": [ROLE_USER], "editor": [ROLE_USER, ROLE_EDITOR],
                "approver": [ROLE_USER, ROLE_EDITOR, ROLE_APPROVER],
                "admin": [ROLE_USER, ROLE_EDITOR, ROLE_APPROVER, ROLE_ADMIN]}


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
    print(f"  Setup URI  : {totp_uri(secret, username, ORG_NAME)}")
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
                print(f"{k.id}  {k.prefix}…  {state:7}  {k.name}  roles={k.roles or ['MSPTools.User']} "
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
        if args.role == "user" and any(s.endswith(":write") for s in scopes):
            sys.exit("Write scopes need --role editor")
        secret = "msp_" + secrets.token_urlsafe(32)
        roles = [ROLE_USER] + ([ROLE_EDITOR] if args.role == "editor" else [])
        db.add(ApiKey(id=str(uuid4()), name=args.name.strip()[:255], prefix=secret[:10],
                      key_hash=hashlib.sha256(secret.encode()).hexdigest(), revoked=False,
                      created_at=datetime.now(UTC), roles=roles, scopes=scopes, created_by="cli",
                      expires_at=datetime.now(UTC) + timedelta(days=args.expires_days) if args.expires_days else None))
        await db.commit()
        print(f"Key for '{args.name}' (roles {roles}, scopes {scopes}) — shown once, store it securely:\n\n  {secret}\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="MSP API Tools admin commands")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-master-key", help="Print a new MSP_MASTER_KEY value")
    sub.add_parser("list-users", help="List local accounts")
    c = sub.add_parser("create-user", help="Create a local account (password + authenticator)")
    c.add_argument("username")
    c.add_argument("--role", choices=sorted(ROLE_ALIASES), default="user")
    for name in ("reset-totp", "set-password", "disable-user", "enable-user"):
        sub.add_parser(name).add_argument("username")
    k = sub.add_parser("create-key", help="Create a connection key for a program / AI client")
    k.add_argument("name")
    k.add_argument("--role", choices=["user", "editor"], default="user")
    k.add_argument("--scope", action="append", help="'*:read' (default), 'connectwise_psa:write', … (repeatable)")
    k.add_argument("--expires-days", type=int, choices=range(1, 731), metavar="1-730")
    sub.add_parser("list-keys", help="List connection keys")
    sub.add_parser("revoke-key", help="Revoke a key and its OAuth tokens").add_argument("key_id")
    sub.add_parser("setup-code", help="Print the code the first-run /setup page asks for")
    sub.add_parser("migrate", help="Bring the database schema up to date (also runs at app startup)")
    args = parser.parse_args(argv)
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
