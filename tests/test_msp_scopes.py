"""Scopes limit the MSP pack too, not just core connections (app/packs/msp: ``*:read``, ``*:write`` for Editor routes).

A connection key scoped to one connection (``petstore:read``) must not reach /msp/mcp tools or the pack's REST API;
``*:read`` reads, an Editor ``*:write`` key still writes, and signed-in people are unaffected.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import ADMIN_ROLES, make_session

VIEWER = ["MSPTools.User"]
NEEDS_READ = "This credential's scopes don't include the MSP tools (needs *:read)"


def _key(client, factory, **body) -> dict[str, str]:  # type: ignore[no-untyped-def]
    client.cookies.set("msp_session", make_session(factory, ADMIN_ROLES, "admin@example.com"))
    r = client.post("/api/admin/api-keys", json={"name": "k", **body})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return {"X-API-Key": r.json()["key"]}


def _call(client, headers, tool: str, arguments: dict | None = None) -> dict:  # type: ignore[no-untyped-def]
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments or {}}}
    r = client.post("/msp/mcp", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["result"]


@pytest.fixture
def no_vendor_sync(monkeypatch):  # type: ignore[no-untyped-def]
    async def _sync(session, connectors):  # type: ignore[no-untyped-def]
        return {"platforms": {}}

    async def _link(session):  # type: ignore[no-untyped-def]
        return {"linked": 0}

    monkeypatch.setattr("app.api.clients.sync_platform_companies", _sync)
    monkeypatch.setattr("app.api.clients.auto_link", _link)


def test_platform_scoped_key_is_refused_by_the_msp_pack(app_client, db_factory):  # type: ignore[no-untyped-def]
    h = _key(app_client, db_factory, role="reader", scopes=["petstore:read"])
    for tool in ("list_clients", "clients_summary", "list_changes", "search_tickets", "find_workflow"):
        result = _call(app_client, h, tool, {"query": "x"} if tool == "find_workflow" else None)
        assert result["isError"] is True and result["content"][0]["text"] == NEEDS_READ, (tool, result)
    for path in ("/api/clients", "/api/clients/summary", "/api/db/summary", "/api/tickets", "/api/av/gaps",
                 "/api/discrepancies", "/api/helpdesk/alerts", "/api/onboarding/catalog", "/msp/plugin/manifest"):
        r = app_client.get(path, headers=h)
        assert r.status_code == 403 and r.json()["detail"] == NEEDS_READ, (path, r.text)
    # proposals of MSP commands and the pack's propose-only routes too
    r = app_client.post("/api/changes", json={"command": "save_workflow", "params": {}}, headers=h)
    assert r.status_code == 403 and r.json()["detail"] == NEEDS_READ
    assert app_client.post("/api/av/sweep", json={}, headers=h).status_code == 403
    # tools/list still works (the check is on the call, not by hiding tools)
    listed = app_client.post("/msp/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=h)
    assert listed.status_code == 200 and listed.json()["result"]["tools"]
    # a single vendor's scope isn't enough either: the pack mixes every vendor's data
    psa = _key(app_client, db_factory, role="reader", scopes=["connectwise_psa:read"])
    assert app_client.get("/api/clients", headers=psa).status_code == 403


def test_vendor_proxy_checks_that_platforms_own_scope(app_client, db_factory):  # type: ignore[no-untyped-def]
    h = _key(app_client, db_factory, role="reader", scopes=["petstore:read"])
    r = app_client.get("/api/proxy/connectwise_psa/company/companies", headers=h)
    assert r.status_code == 403 and "connectwise_psa:read" in r.text


def test_star_read_key_reads_the_msp_pack(app_client, db_factory):  # type: ignore[no-untyped-def]
    h = _key(app_client, db_factory, role="reader", scopes=["*:read"])
    assert app_client.get("/api/clients", headers=h).status_code == 200
    assert app_client.get("/api/db/summary", headers=h).status_code == 200
    result = _call(app_client, h, "clients_summary")
    assert result["isError"] is False, result
    # still a Reader: Editor tools say so, not "missing scope"
    result = _call(app_client, h, "refresh_client_registry")
    assert result["isError"] is True and "Editor" in result["content"][0]["text"]


def test_editor_write_key_runs_msp_writes(app_client, db_factory, no_vendor_sync):  # type: ignore[no-untyped-def]
    h = _key(app_client, db_factory, role="editor", scopes=["*:write"])
    result = _call(app_client, h, "refresh_client_registry")
    assert result["isError"] is False, result
    assert app_client.post("/api/clients/sync", headers=h).status_code == 200


def test_editor_key_without_star_write_cannot_write_through_the_pack(app_client, db_factory):  # type: ignore[no-untyped-def]
    h = _key(app_client, db_factory, role="editor", scopes=["*:read", "petstore:write"])
    assert app_client.get("/api/clients", headers=h).status_code == 200
    r = app_client.post("/api/clients/sync", headers=h)
    assert r.status_code == 403 and "needs *:write" in r.json()["detail"]
    result = _call(app_client, h, "refresh_client_registry")
    assert result["isError"] is True and "needs *:write" in result["content"][0]["text"]


def test_signed_in_people_are_unaffected(app_client, db_factory, no_vendor_sync):  # type: ignore[no-untyped-def]
    app_client.cookies.set("msp_session", make_session(db_factory, VIEWER, "viewer@example.com"))
    assert app_client.get("/api/clients", headers={}).status_code == 200
    assert _call(app_client, {}, "clients_summary")["isError"] is False
    app_client.cookies.set("msp_session", make_session(db_factory, ["MSPTools.User", "MSPTools.Editor"],
                                                       "editor@example.com"))
    assert app_client.post("/api/clients/sync").status_code == 200


def test_default_oauth_token_for_msp_mcp_still_calls_tools(app_client, db_factory):  # type: ignore[no-untyped-def]
    from app.api.external_auth import default_resource
    from app.db.schema import OAuthToken

    token = secrets.token_urlsafe(32)

    async def _add() -> None:
        async with db_factory() as db:
            db.add(OAuthToken(token_hash=hashlib.sha256(token.encode()).hexdigest(), kind="access",
                              client_id="c_test", api_key_id="m365:oid-1", user_upn="sso@example.com",
                              user_roles=["MSPTools.User"], resource=default_resource(),
                              expires_at=datetime.now(UTC) + timedelta(hours=1), created_at=datetime.now(UTC)))
            await db.commit()

    asyncio.run(_add())
    result = _call(app_client, {"Authorization": f"Bearer {token}"}, "clients_summary")
    assert result["isError"] is False, result
