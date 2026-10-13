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
    assert step1["totp_qr"].startswith("data:image/svg+xml")  # drawn locally, no outside QR service
    assert _users(db_factory) == []  # nothing saved until the authenticator is proven

    wrong = app_client.post("/setup/admin", json={**body, "totp_secret": step1["totp_secret"], "totp_code": "000000"})
    assert wrong.status_code == 400
    done = app_client.post("/setup/admin", json={**body, "totp_secret": step1["totp_secret"],
                                                 "totp_code": totp_now(step1["totp_secret"])}).json()
    assert done["step"] == "done"
    [user] = _users(db_factory)
    assert user.username == "admin" and user.roles == ["Admin"] and user.password_hash.startswith("scrypt$")

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


def test_sign_in_page_sends_a_fresh_install_to_setup(app_client):  # type: ignore[no-untyped-def]
    r = app_client.get("/auth/local", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/setup"


def test_first_question_saves_the_pack_choice(app_client, db_factory, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from tests.conftest import ADMIN_ROLES, make_session

    monkeypatch.delenv("MSP_PACKS")
    monkeypatch.setenv("MSP_DATA_DIR", str(tmp_path))
    app_client.cookies.set("msp_session", make_session(db_factory, ADMIN_ROLES))
    assert app_client.get("/api/admin/getting-started").json()["step"] == "secure"
    app_client.post("/api/admin/secure/step", json={"mode": "local"})
    gs = app_client.get("/api/admin/getting-started").json()
    assert gs["step"] == "choose" and not gs["chosen"]

    r = app_client.post("/api/admin/packs", json={"packs": []})
    # Not running under `app.cli run` here, so the rebuild has to be done by hand
    assert r.status_code == 200 and r.json() == {"packs": [], "restart": "manual"}
    assert (tmp_path / "settings.json").exists()
    assert app_client.get("/api/admin/getting-started").json()["chosen"]
    assert app_client.post("/api/admin/packs", json={"packs": ["nope"]}).status_code == 400

    from app import packs

    assert packs.configured() == []
    monkeypatch.setenv("MSP_PACKS", "msp")  # .env decides
    assert app_client.post("/api/admin/packs", json={"packs": []}).status_code == 409


def test_restart_handler_is_used_when_running_locally(app_client, db_factory, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from app import runtime
    from tests.conftest import ADMIN_ROLES, make_session

    monkeypatch.delenv("MSP_PACKS")
    monkeypatch.setenv("MSP_DATA_DIR", str(tmp_path))
    app_client.cookies.set("msp_session", make_session(db_factory, ADMIN_ROLES))
    called = []
    runtime.set_restart_handler(lambda: called.append(True))
    try:
        r = app_client.post("/api/admin/packs", json={"packs": []})
    finally:
        runtime.set_restart_handler(None)
    assert r.json()["restart"] == "automatic" and called == [True]
