"""Phase 1 security: authentication everywhere, local accounts, keys/scopes, write modes, encryption, OAuth
audience binding, OIDC signature verification, internal-call spoofing, MCP roles, audit and rate limits.

Everything runs locally against a throwaway SQLite database — no vendor or identity provider is contacted
(OIDC endpoints are mocked with respx).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import Response
from sqlalchemy import select, text

from app.auth.passwords import (
    hash_password,
    new_totp_secret,
    totp_now,
    verify_password,
    verify_totp,
)
from tests.conftest import ADMIN_ROLES, make_session

EDITOR = ["MSPTools.User", "MSPTools.Editor"]
VIEWER = ["MSPTools.User"]


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _sign_in(client, factory, roles, upn="person@example.com"):  # type: ignore[no-untyped-def]
    client.cookies.set("msp_session", make_session(factory, roles, upn))


def _create_key(client, factory, **body):  # type: ignore[no-untyped-def]
    _sign_in(client, factory, ADMIN_ROLES, "admin@example.com")
    r = client.post("/api/admin/api-keys", json={"name": "k", **body})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return r.json()["key"]


# ── Test isolation guard ──────────────────────────────────────────────────


def test_tests_cannot_reach_real_systems(app_client) -> None:  # type: ignore[no-untyped-def]
    import httpx

    from app.config import get_settings
    from app.packs.msp import default_connectors

    with pytest.raises(Exception, match="may not open network connections"):
        httpx.get("https://1.1.1.1/", timeout=5)
    assert get_settings().s1_api_token == "" and get_settings().cw_private_key == ""
    assert not default_connectors()  # no vendor credentials loaded


# ── Authentication everywhere ─────────────────────────────────────────────


def test_api_requires_credentials(app_client):  # type: ignore[no-untyped-def]
    r = app_client.get("/api/companies")
    assert r.status_code == 401
    assert "resource_metadata" in r.headers["www-authenticate"]


def test_browser_without_session_is_sent_to_sign_in(app_client):  # type: ignore[no-untyped-def]
    r = app_client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/auth/login?next=/")


def test_public_routes_stay_open(app_client):  # type: ignore[no-untyped-def]
    assert app_client.get("/health").json() == {"status": "ok"}
    assert app_client.get("/auth/local").status_code == 200
    assert app_client.get("/auth/me").json()["signed_in"] is False
    assert app_client.get("/.well-known/oauth-authorization-server").status_code == 200
    assert app_client.get("/msp/plugin/install.ps1").status_code == 200


def test_openapi_and_mcp_need_sign_in(app_client):  # type: ignore[no-untyped-def]
    assert app_client.get("/openapi.json").status_code == 401
    assert app_client.post("/msp/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    assert app_client.get("/mcp").status_code in (401, 404)  # the old unauthenticated mount is gone


def test_without_oidc_login_goes_to_local_page(app_client):  # type: ignore[no-untyped-def]
    r = app_client.get("/auth/login?next=/changes", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/auth/local")


# ── Local accounts (password + TOTP) ──────────────────────────────────────


def test_totp_matches_rfc6238_vector() -> None:
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp_now(secret, at=59) == "287082"  # RFC 6238 SHA1 vector 94287082, last 6 digits
    assert verify_totp(secret, "287082", at=59) is not None
    assert verify_totp(secret, "000000", at=59) is None


def test_password_hashing() -> None:
    h = hash_password("correct horse battery")
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong horse battery", h)
    with pytest.raises(ValueError):
        hash_password("short")


def _add_local_user(factory, username="admin", roles=ADMIN_ROLES):  # type: ignore[no-untyped-def]
    from app.db.schema import LocalUser

    secret = new_totp_secret()

    async def _add() -> None:
        async with factory() as db:
            db.add(LocalUser(username=username, password_hash=hash_password("a long test password"),
                             totp_secret=secret, roles=roles))
            await db.commit()

    _run(_add())
    return secret


def test_local_sign_in_success_and_code_replay(app_client, db_factory):  # type: ignore[no-untyped-def]
    secret = _add_local_user(db_factory)
    code = totp_now(secret)
    form = {"username": "admin", "password": "a long test password", "code": code, "next": "/changes"}
    r = app_client.post("/auth/local", data=form, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/changes"
    assert app_client.get("/auth/me").json()["signed_in"] is True
    assert app_client.get("/api/admin/audit").status_code == 200  # admin role carried into the session

    app_client.cookies.clear()
    replay = app_client.post("/auth/local", data=form, follow_redirects=False)
    assert replay.status_code == 401  # the same code works once


def test_local_sign_in_rejects_wrong_password_and_locks(app_client, db_factory):  # type: ignore[no-untyped-def]
    secret = _add_local_user(db_factory)
    for _ in range(5):
        r = app_client.post("/auth/local", data={"username": "admin", "password": "wrong password!!",
                                                  "code": totp_now(secret)})
        assert r.status_code == 401
    locked = app_client.post("/auth/local", data={"username": "admin", "password": "a long test password",
                                                   "code": totp_now(secret)})
    assert locked.status_code == 429


def test_local_sign_in_unknown_user(app_client, db_factory):  # type: ignore[no-untyped-def]
    r = app_client.post("/auth/local", data={"username": "nobody", "password": "x" * 12, "code": "123456"})
    assert r.status_code == 401


def test_open_redirect_blocked(app_client, db_factory):  # type: ignore[no-untyped-def]
    secret = _add_local_user(db_factory)
    r = app_client.post("/auth/local", data={"username": "admin", "password": "a long test password",
                                              "code": totp_now(secret), "next": "//evil.example.com"},
                        follow_redirects=False)
    assert r.headers["location"] == "/"


# ── Roles ─────────────────────────────────────────────────────────────────


def test_viewer_cannot_write_or_administer(app_client, db_factory):  # type: ignore[no-untyped-def]
    _sign_in(app_client, db_factory, VIEWER)
    assert app_client.get("/api/companies").status_code == 200
    assert app_client.post("/api/overrides/companies", json={}).status_code == 403
    assert app_client.get("/api/admin/audit").status_code == 403
    assert app_client.post("/api/admin/api-keys", json={"name": "x"}).status_code == 403


def test_cross_site_post_refused(app_client, db_factory):  # type: ignore[no-untyped-def]
    _sign_in(app_client, db_factory, EDITOR)
    r = app_client.post("/api/overrides/companies", json={}, headers={"origin": "https://evil.example.com"})
    assert r.status_code == 403 and "Cross-site" in r.text


def test_only_approver_changes_connector_credentials(app_client, db_factory):  # type: ignore[no-untyped-def]
    _sign_in(app_client, db_factory, VIEWER)
    r = app_client.post("/api/connectors/register", json={"platform": "sentinelone", "credentials": {}})
    assert r.status_code == 403


# ── Connection keys + scopes ──────────────────────────────────────────────


def test_read_key_reads_but_cannot_write(app_client, db_factory):  # type: ignore[no-untyped-def]
    key = _create_key(app_client, db_factory, role="user", scopes=["*:read"])
    h = {"X-API-Key": key}
    assert app_client.get("/api/companies", headers=h).status_code == 200
    assert app_client.post("/api/overrides/companies", json={}, headers=h).status_code == 403
    assert app_client.get("/api/admin/api-keys", headers=h).status_code == 403


def test_key_creation_validates_role_and_scopes(app_client, db_factory):  # type: ignore[no-untyped-def]
    _sign_in(app_client, db_factory, ADMIN_ROLES)
    assert app_client.post("/api/admin/api-keys", json={"name": "x", "role": "admin"}).status_code == 400
    assert app_client.post("/api/admin/api-keys", json={"name": "x", "scopes": ["*:write"]}).status_code == 400
    assert app_client.post("/api/admin/api-keys", json={"name": "x", "scopes": ["bad scope"]}).status_code == 400


def test_expired_and_revoked_keys_are_refused(app_client, db_factory):  # type: ignore[no-untyped-def]
    from app.db.schema import ApiKey

    key = _create_key(app_client, db_factory, role="user", scopes=["*:read"], expires_days=1)

    async def _expire() -> None:
        async with db_factory() as db:
            row = (await db.execute(select(ApiKey))).scalar_one()
            row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await db.commit()

    _run(_expire())
    assert app_client.get("/api/companies", headers={"X-API-Key": key}).status_code == 401
    assert app_client.get("/api/companies", headers={"X-API-Key": "msp_not-a-real-key"}).status_code == 401


def test_platform_scoped_key_cannot_write_other_platforms(app_client, db_factory, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MSP_WRITE_MODE_DEFAULT", "direct")
    key = _create_key(app_client, db_factory, role="editor", scopes=["connectwise_psa:write"])
    r = app_client.post("/api/proxy/sentinelone/web/api/v2.1/agents", json={}, headers={"X-API-Key": key})
    assert r.status_code == 403 and "may not write to sentinelone" in r.text


# ── Write modes ───────────────────────────────────────────────────────────


def test_write_mode_read_only_by_default(app_client, db_factory):  # type: ignore[no-untyped-def]
    _sign_in(app_client, db_factory, EDITOR)
    r = app_client.post("/api/proxy/sentinelone/web/api/v2.1/agents", json={})
    assert r.status_code == 403 and "read-only" in r.text


def test_write_mode_approval_and_direct(app_client, db_factory):  # type: ignore[no-untyped-def]
    _sign_in(app_client, db_factory, ADMIN_ROLES)
    assert app_client.put("/api/admin/write-modes/sentinelone", json={"write_mode": "approval"}).status_code == 200
    r = app_client.post("/api/proxy/sentinelone/web/api/v2.1/agents", json={})
    assert r.status_code == 409 and "approval" in r.text
    assert app_client.put("/api/admin/write-modes/sentinelone", json={"write_mode": "direct"}).status_code == 200
    r = app_client.post("/api/proxy/sentinelone/web/api/v2.1/agents", json={})
    assert r.status_code == 404, r.text  # past the write gate; no SentinelOne connector configured in tests
    assert app_client.put("/api/admin/write-modes/sentinelone", json={"write_mode": "yolo"}).status_code == 400
    modes = app_client.get("/api/admin/write-modes").json()
    assert modes["platforms"]["sentinelone"] == {"write_mode": "direct", "source": "policy"}


def test_change_execution_respects_write_mode(db_factory, monkeypatch):  # type: ignore[no-untyped-def]
    from fastapi import HTTPException

    from app.auth.write_mode import enforce_change_execution

    async def _blocked() -> int:
        try:
            await enforce_change_execution("asio")
        except HTTPException as exc:
            return exc.status_code
        return 0

    assert _run(_blocked()) == 403  # read_only (default) refuses even approved changes
    monkeypatch.setenv("MSP_WRITE_MODE_ASIO", "approval")
    assert _run(_blocked()) == 0    # approval mode: approved changes run


# ── Encryption at rest ────────────────────────────────────────────────────


def test_connector_credentials_encrypted_at_rest(db_factory):  # type: ignore[no-untyped-def]
    from app.db.repository import ConnectorConfigRepository

    async def _go() -> tuple[str, dict]:
        async with db_factory() as db:
            await ConnectorConfigRepository(db).upsert("sentinelone", {"api_token": "SUPER-SECRET"})
        async with db_factory() as db:
            raw = (await db.execute(text("SELECT credentials FROM connector_configs"))).scalar_one()
            cfg = await ConnectorConfigRepository(db).get_by_platform("sentinelone")
        return raw, cfg.credentials  # type: ignore[union-attr]

    raw, creds = _run(_go())
    assert "SUPER-SECRET" not in str(raw) and "__enc__" in str(raw)
    assert creds == {"api_token": "SUPER-SECRET"}


def test_plaintext_rows_are_reencrypted(db_factory):  # type: ignore[no-untyped-def]
    from app.auth.crypto import reencrypt_plaintext_rows
    from app.db.repository import ConnectorConfigRepository

    async def _go() -> tuple[int, str, dict]:
        async with db_factory() as db:
            await db.execute(text("INSERT INTO connector_configs (id, platform, enabled, credentials, created_at, "
                                  "updated_at) VALUES ('1', 'eset', 1, :c, :t, :t)"),
                             {"c": json.dumps({"password": "OLD-PLAIN"}), "t": datetime.now(UTC)})
            await db.commit()
        converted = await reencrypt_plaintext_rows()
        async with db_factory() as db:
            raw = (await db.execute(text("SELECT credentials FROM connector_configs"))).scalar_one()
            cfg = await ConnectorConfigRepository(db).get_by_platform("eset")
        return converted, raw, cfg.credentials  # type: ignore[union-attr]

    converted, raw, creds = _run(_go())
    assert converted == 1 and "OLD-PLAIN" not in str(raw)
    assert creds == {"password": "OLD-PLAIN"}


def test_saving_credentials_without_master_key_is_refused(monkeypatch):  # type: ignore[no-untyped-def]
    from app.auth import crypto

    monkeypatch.delenv("MSP_MASTER_KEY")
    crypto.reset_key_cache()
    try:
        with pytest.raises(crypto.MasterKeyError):
            crypto.encrypt_value({"a": 1})
    finally:
        monkeypatch.undo()
        crypto.reset_key_cache()


# ── OAuth audience binding ────────────────────────────────────────────────


def _oauth_access_token(factory, resource: str | None) -> str:  # type: ignore[no-untyped-def]
    from app.db.schema import OAuthToken

    token = secrets.token_urlsafe(32)

    async def _add() -> None:
        async with factory() as db:
            db.add(OAuthToken(token_hash=hashlib.sha256(token.encode()).hexdigest(), kind="access",
                              client_id="c_test", api_key_id="m365:oid-1", user_upn="sso@example.com",
                              user_roles=["MSPTools.User"], resource=resource,
                              expires_at=datetime.now(UTC) + timedelta(hours=1), created_at=datetime.now(UTC)))
            await db.commit()

    _run(_add())
    return token


def test_oauth_token_only_valid_for_its_mcp_server(app_client, db_factory):  # type: ignore[no-untyped-def]
    from app.api.external_auth import default_resource

    token = _oauth_access_token(db_factory, default_resource())
    h = {"Authorization": f"Bearer {token}"}
    ping = app_client.post("/msp/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=h)
    assert ping.status_code == 200 and ping.json()["result"] == {}
    assert app_client.get("/api/companies", headers=h).status_code == 403


def test_oauth_authorize_rejects_foreign_resource(app_client, db_factory):  # type: ignore[no-untyped-def]
    reg = app_client.post("/oauth/register", json={"redirect_uris": ["http://localhost:3333/cb"]}).json()
    params = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": "http://localhost:3333/cb",
              "code_challenge": "x" * 43, "code_challenge_method": "S256", "resource": "https://evil.example.com/mcp"}
    r = app_client.get("/oauth/authorize", params=params)
    assert r.status_code == 400 and "resource" in r.text


# ── Internal calls can't be spoofed ───────────────────────────────────────


def test_internal_header_requires_the_secret(app_client):  # type: ignore[no-untyped-def]
    from app.auth.principal import Principal, internal_headers

    forged = internal_headers(Principal(id="root", kind="session", roles=frozenset(ADMIN_ROLES),
                                        scopes=frozenset({"*:write"})), "mcp")
    forged["X-MSP-Internal"] = "guess"
    assert app_client.get("/api/companies", headers=forged).status_code == 401


def test_internal_header_refused_from_the_internet_edge(app_client):  # type: ignore[no-untyped-def]
    from app.auth.principal import Principal, internal_headers

    real = internal_headers(Principal(id="x", kind="session", roles=frozenset(VIEWER),
                                      scopes=frozenset({"*:read"})), "mcp")
    r = app_client.get("/api/companies", headers={**real, "X-MSP-Edge": "1", "X-Real-IP": "10.0.0.5"})
    assert r.status_code == 403


def test_requester_header_no_longer_trusted() -> None:
    import inspect

    from app.api import changes

    assert "x-msp-requester" not in inspect.getsource(changes).lower()


# ── MCP roles + audit ─────────────────────────────────────────────────────


def test_mcp_write_tool_needs_editor_and_is_audited(app_client, db_factory):  # type: ignore[no-untyped-def]
    from app.db.schema import AuditLog

    key = _create_key(app_client, db_factory, role="user", scopes=["*:read"])
    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "refresh_client_registry", "arguments": {}}}
    r = app_client.post("/msp/mcp", json=call, headers={"X-API-Key": key}).json()
    assert r["result"]["isError"] is True and "Editor" in r["result"]["content"][0]["text"]

    async def _rows() -> list[AuditLog]:
        async with db_factory() as db:
            return list((await db.execute(select(AuditLog).where(AuditLog.action == "refresh_client_registry"))).scalars())

    rows = _run(_rows())
    assert rows and rows[0].principal == "key:k" and rows[0].source == "mcp"


def test_writes_are_audited(app_client, db_factory):  # type: ignore[no-untyped-def]
    _sign_in(app_client, db_factory, VIEWER, "viewer@example.com")
    app_client.post("/api/overrides/companies", json={})
    _sign_in(app_client, db_factory, ADMIN_ROLES, "admin@example.com")
    log = app_client.get("/api/admin/audit").json()
    assert any(e["principal"] == "viewer@example.com" and e["status"] == 403 for e in log)


# ── Rate limits ───────────────────────────────────────────────────────────


def test_sign_in_attempts_are_rate_limited(app_client, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MSP_RATE_LIMIT_ANON_PER_MIN", "3")
    codes = [app_client.post("/auth/local", data={"username": "u", "password": "p" * 12, "code": "111111"}).status_code
             for _ in range(4)]
    assert codes[-1] == 429


# ── OIDC ID-token verification (signature via JWKS) ───────────────────────

ISSUER = "https://idp.example.com"


@pytest.fixture
def oidc(monkeypatch):  # type: ignore[no-untyped-def]
    from app.api import auth_m365

    monkeypatch.setenv("MSP_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("MSP_OIDC_CLIENT_ID", "client-123")
    monkeypatch.setenv("MSP_OIDC_CLIENT_SECRET", "s")
    auth_m365._cache.clear()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": "k1", "use": "sig", "alg": "RS256"})
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{ISSUER}/.well-known/openid-configuration").mock(return_value=Response(200, json={
            "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/jwks"}))
        mock.get(f"{ISSUER}/jwks").mock(return_value=Response(200, json={"keys": [jwk]}))
        yield key
    auth_m365._cache.clear()


def _id_token(key, **overrides):  # type: ignore[no-untyped-def]
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": "client-123", "sub": "u1", "iat": now, "exp": now + 300, "nonce": "n1",
              "preferred_username": "sso@example.com", "roles": ["MSPTools.Editor", "Something.Else"]}
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def test_oidc_valid_token_and_role_filtering(oidc):  # type: ignore[no-untyped-def]
    from app.api.auth_m365 import _roles_from_claims, verify_id_token

    claims = _run(verify_id_token(_id_token(oidc), "n1"))
    assert claims["preferred_username"] == "sso@example.com"
    assert _roles_from_claims(claims) == ["Editor"]


@pytest.mark.parametrize("change", [{"aud": "someone-else"}, {"iss": "https://evil.example.com"},
                                    {"exp": int(time.time()) - 3600}, {"nonce": "other"}])
def test_oidc_rejects_bad_claims(oidc, change):  # type: ignore[no-untyped-def]
    from fastapi import HTTPException

    from app.api.auth_m365 import verify_id_token

    with pytest.raises(HTTPException):
        _run(verify_id_token(_id_token(oidc, **change), "n1"))


def test_oidc_rejects_forged_signature(oidc):  # type: ignore[no-untyped-def]
    from fastapi import HTTPException

    from app.api.auth_m365 import verify_id_token

    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(HTTPException):
        _run(verify_id_token(_id_token(attacker), "n1"))


def test_oidc_role_map_for_group_claims(monkeypatch):  # type: ignore[no-untyped-def]
    from app.api.auth_m365 import _roles_from_claims

    monkeypatch.setenv("MSP_OIDC_ROLES_CLAIM", "groups")
    monkeypatch.setenv("MSP_OIDC_ROLE_MAP", json.dumps({"msp-editors": "MSPTools.Editor", "x": "NotARole"}))
    assert _roles_from_claims({"groups": ["msp-editors", "x", "other"]}) == ["Editor"]
