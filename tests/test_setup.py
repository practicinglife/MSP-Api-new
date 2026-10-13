"""Phase 5: first-run setup (first admin, guarded by the setup code) and the admin checklist."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from tests.conftest import ADMIN_ROLES, make_session

VIEWER = ["MSPTools.User"]


def _users(factory) -> list:  # type: ignore[no-untyped-def]
    from app.db.schema import LocalUser

    async def _q() -> list:
        async with factory() as db:
            return list((await db.execute(select(LocalUser))).scalars())

    return asyncio.run(_q())


def test_first_admin(app_client, db_factory) -> None:  # type: ignore[no-untyped-def]
    from app.api.setup import setup_code
    from app.auth.passwords import totp_now

    assert app_client.get("/setup").status_code == 200  # public page
    assert app_client.get("/setup/status").json() == {"needs_setup": True, "master_key": True}
    body = {"setup_code": "AAAA-BBBB-CCCC", "username": "admin", "password": "a-long-password-1"}
    assert app_client.post("/setup/admin", json=body).status_code == 403  # wrong setup code

    body["setup_code"] = setup_code().lower()  # case and dashes don't matter
    assert app_client.post("/setup/admin", json={**body, "password": "short"}).status_code == 400
    assert app_client.post("/setup/admin", json={**body, "username": "Bad Name!"}).status_code == 400
    step1 = app_client.post("/setup/admin", json=body).json()
    assert step1["step"] == "authenticator" and step1["totp_uri"].startswith("otpauth://totp/")
    assert _users(db_factory) == []  # nothing saved until the authenticator is proven

    wrong = app_client.post("/setup/admin", json={**body, "totp_secret": step1["totp_secret"], "totp_code": "000000"})
    assert wrong.status_code == 400
    done = app_client.post("/setup/admin", json={**body, "totp_secret": step1["totp_secret"],
                                                 "totp_code": totp_now(step1["totp_secret"])}).json()
    assert done["step"] == "done"
    [user] = _users(db_factory)
    assert user.username == "admin" and "MSPTools.Admin" in user.roles and user.password_hash.startswith("scrypt$")

    assert app_client.get("/setup/status").json() == {"needs_setup": False}
    again = app_client.post("/setup/admin", json=body)
    assert again.status_code == 409  # can't be used twice


def test_setup_needs_the_master_key(app_client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.auth import crypto

    monkeypatch.delenv("MSP_MASTER_KEY")
    crypto.reset_key_cache()
    try:
        s = app_client.get("/setup/status").json()
        assert s["master_key"] is False and len(s["suggested_master_key"]) >= 40
        r = app_client.post("/setup/admin", json={"setup_code": "x", "username": "admin", "password": "a-long-password-1"})
        assert r.status_code == 409 and "MSP_MASTER_KEY" in r.text
    finally:
        monkeypatch.undo()
        crypto.reset_key_cache()


def test_setup_is_not_reachable_from_the_internet(app_client) -> None:  # type: ignore[no-untyped-def]
    edge = {"X-MSP-Edge": "1", "X-Real-IP": "8.8.4.4"}  # a public address (documentation ranges count as private)
    assert app_client.get("/setup", headers=edge).status_code == 404
    assert app_client.post("/setup/admin", json={}, headers=edge).status_code == 404


def test_checklist_is_for_admins(app_client, db_factory) -> None:  # type: ignore[no-untyped-def]
    assert app_client.get("/api/admin/setup").status_code == 401
    app_client.cookies.set("msp_session", make_session(db_factory, VIEWER, "viewer@example.com"))
    assert app_client.get("/api/admin/setup").status_code == 403
    app_client.cookies.set("msp_session", make_session(db_factory, ADMIN_ROLES, "admin@example.com"))
    c = app_client.get("/api/admin/setup").json()
    items = {i["key"]: i for i in c["items"]}
    assert set(items) >= {"master_key", "sign_in", "https", "external_url", "database", "packs", "write_mode",
                          "connections"}
    assert items["master_key"]["ok"] is True and "fix" not in items["master_key"]
    assert items["https"]["ok"] is False  # the test client talks plain http
    assert "msp" in items["packs"]["detail"]
    assert c["needs_attention"] >= 1
