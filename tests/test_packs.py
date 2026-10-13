"""Phase 5: the MSP pack is optional. A core-only install has connections, MCP, local copies, sign-in and
changes, and nothing of the MSP pack is reachable or running."""

from __future__ import annotations

import unittest.mock as mock

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ADMIN_ROLES, make_session


@pytest.fixture
def core_client(db_factory, monkeypatch):  # type: ignore[no-untyped-def]
    """An app built without packs (as a fresh install starts)."""
    from app import packs
    from app.main import create_app

    started: list[str] = []

    async def _no_init(database_url: str | None = None) -> None:
        pass

    async def _msp_startup(app) -> None:  # type: ignore[no-untyped-def]
        started.append("msp")

    previous = packs.active()
    monkeypatch.setattr("app.packs.msp.startup", _msp_startup)
    try:
        app = create_app([])
        with mock.patch("app.main.init_db", side_effect=_no_init), TestClient(app) as client:
            client.cookies.set("msp_session", make_session(db_factory, ADMIN_ROLES, "admin@example.com"))
            client.started = started  # type: ignore[attr-defined]
            yield client
    finally:
        packs.set_active(previous)


def test_core_only_install(core_client) -> None:  # type: ignore[no-untyped-def]
    c = core_client
    assert c.started == []  # the MSP pack's connectors and background jobs never start
    assert c.get("/api/packs").json()["enabled"] == []
    home = c.get("/", follow_redirects=False)
    assert home.status_code == 302 and home.headers["location"] == "/connections"
    assert c.get("/connections").status_code == 200

    # core features
    assert c.get("/api/connections").status_code == 200
    assert c.get("/api/search/operations?q=x").status_code == 200
    assert c.get("/api/changes").status_code == 200
    assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 200
    assert c.get("/api/admin/write-modes").status_code == 200
    assert c.get("/setup").status_code == 200

    # MSP pack features are not there
    for path in ("/api/companies", "/api/clients", "/api/tickets", "/api/av/gaps", "/api/discrepancies",
                 "/library", "/msp/connect"):
        assert c.get(path).status_code == 404, path
    assert c.post("/msp/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 404
    assert c.post("/api/reconcile").status_code == 404


def test_msp_pack_on(test_client) -> None:  # type: ignore[no-untyped-def]
    info = test_client.get("/api/packs").json()
    assert info["enabled"] == ["msp"]
    msp = next(p for p in info["packs"] if p["name"] == "msp")
    assert msp["enabled"] and msp["nav"][0]["href"] == "/"
    assert "<html" in test_client.get("/").text.lower()  # the MSP dashboard
    assert test_client.get("/api/clients").status_code == 200
    names = {c["name"] for c in test_client.get("/api/commands").json()}
    assert "connection_call" in names and len(names) > 1  # vendor commands come with the pack


def test_pack_setting_parsing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app import packs

    monkeypatch.setenv("MSP_PACKS", " MSP , bogus,msp")
    assert packs.configured() == ["msp"]
    monkeypatch.setenv("MSP_PACKS", "")
    assert packs.configured() == []
    monkeypatch.setenv("MSP_PACKS", "none")
    assert packs.configured() == []
