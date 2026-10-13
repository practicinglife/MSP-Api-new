"""Phase 4b — keep a local copy of any connection's read operations, and search it.

Everything is local: the API is a respx mock, the SQL source a temporary SQLite file. conftest blocks real
network and keeps the background scheduler off (MSP_CONNECTION_SYNC=off).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

import httpx
import respx
from sqlalchemy import select

from tests.conftest import ADMIN_ROLES, make_session

BASE = "https://desk.example.com/api"
EDITOR = ["MSPTools.User", "MSPTools.Editor"]


def _param(name: str, typ: str = "integer", where: str = "query") -> dict[str, Any]:
    return {"name": name, "in": where, "schema": {"type": typ}}


SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Desk", "version": "1"},
    "servers": [{"url": BASE}],
    "paths": {
        "/tickets": {
            "get": {"operationId": "listTickets", "summary": "List tickets",
                    "parameters": [_param("page"), _param("pageSize"), _param("status", "string")]},
            "post": {"operationId": "createTicket", "summary": "Create a ticket",
                     "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}},
        },
        "/devices": {"get": {"operationId": "listDevices", "summary": "List devices",
                             "parameters": [_param("cursor", "string")]}},
        "/companies": {"get": {"operationId": "listCompanies", "summary": "List companies",
                               "parameters": [_param("page")]}},
    },
}

TICKETS = [{"id": i, "summary": f"Ticket {i}", "status": "open" if i % 2 else "closed",
            "company": {"id": 40 + i % 3, "name": ["Acme", "Globex", "Initech"][i % 3]}} for i in range(1, 6)]


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _setup(client) -> None:  # type: ignore[no-untyped-def]
    r = client.post("/api/connections", json={"kind": "openapi", "name": "Desk", "source": {"text": json.dumps(SPEC)},
                                              "auth": {"type": "bearer", "token": "t-1"}})
    assert r.status_code == 200, r.text


def _sync(client, operation: str, **body) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    r = client.post("/api/connections/desk/syncs?run=false", json={"operation": operation, **body})
    assert r.status_code == 200, r.text
    return r.json()


def _run_sync(client, sync_id: str, slug: str = "desk") -> dict[str, Any]:  # type: ignore[no-untyped-def]
    r = client.post(f"/api/connections/{slug}/syncs/{sync_id}/run?wait=true")
    assert r.status_code == 200, r.text
    return r.json()


def _tickets_api(data: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        page, size = int(request.url.params.get("page", 1)), int(request.url.params.get("pageSize", 25))
        return httpx.Response(200, json=data[(page - 1) * size: page * size])
    return handler


# ── paging + change tracking ──────────────────────────────────────────────


def test_page_numbers_and_change_tracking(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    s = _sync(test_client, "list_tickets", paging={"size": 2}, interval_minutes=1)
    assert s["interval_minutes"] == 5  # clamped to the minimum

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{BASE}/tickets").mock(side_effect=_tickets_api(TICKETS))
        out = _run_sync(test_client, s["id"])
    assert out["status"] == "ok", out
    assert out["pages"] == 3 and out["added"] == 5 and out["records"] == 5   # 2 + 2 + 1 (short page → stop)
    assert route.calls[0].request.url.params["pageSize"] == "2"
    assert route.calls[0].request.headers["Authorization"] == "Bearer t-1"   # the connection's own auth

    changed = [dict(t) for t in TICKETS if t["id"] != 5]                      # #5 gone
    changed[0]["status"] = "closed"                                           # #1 changed
    with respx.mock() as mock:
        mock.get(f"{BASE}/tickets").mock(side_effect=_tickets_api(changed))
        out = _run_sync(test_client, s["id"])
    assert (out["added"], out["updated"], out["unchanged"], out["removed"]) == (0, 1, 3, 1)

    listed = test_client.get("/api/connections/desk/syncs").json()[0]
    assert listed["record_count"] == 4 and listed["status"] == "ok" and listed["last_ok_at"]
    assert listed["next_run_at"]

    one = test_client.get("/api/connections/desk/records/1").json()
    assert one["data"]["status"] == "closed" and one["operation"] == "list_tickets"
    gone = test_client.get("/api/connections/desk/records/5").json()
    assert gone["removed_at"]
    assert test_client.get("/api/connections/desk/records?f.id=5").json()["count"] == 0
    assert test_client.get("/api/connections/desk/records?f.id=5&include_removed=true").json()["count"] == 1


def test_cursor_and_link_paging(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    devices = _sync(test_client, "list_devices")
    companies = _sync(test_client, "list_companies")

    def device_pages(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(200, json={"items": [{"uuid": "d1", "name": "PC-ACME-01"}], "next_cursor": "c2"})
        assert cursor == "c2"
        return httpx.Response(200, json={"items": [{"uuid": "d2", "name": "PC-ACME-02"}], "next_cursor": None})

    def company_pages(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        if page == "1":
            return httpx.Response(200, json=[{"id": 1, "name": "Acme"}],
                                  headers={"Link": f'<{BASE}/companies?page=2>; rel="next"'})
        return httpx.Response(200, json=[{"id": 2, "name": "Globex"}])

    with respx.mock() as mock:
        mock.get(f"{BASE}/devices").mock(side_effect=device_pages)
        mock.get(f"{BASE}/companies").mock(side_effect=company_pages)
        d = _run_sync(test_client, devices["id"])
        c = _run_sync(test_client, companies["id"])
    assert d["status"] == "ok" and d["pages"] == 2 and d["records"] == 2
    assert c["status"] == "ok" and c["pages"] == 2 and c["records"] == 2
    assert test_client.get("/api/connections/desk/records/d2").json()["data"]["name"] == "PC-ACME-02"


def test_page_param_ignored_by_api_stops(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    s = _sync(test_client, "list_tickets", paging={"size": 2})
    with respx.mock() as mock:  # always returns the same first page
        mock.get(f"{BASE}/tickets").mock(return_value=httpx.Response(200, json=TICKETS[:2]))
        out = _run_sync(test_client, s["id"])
    assert out["status"] == "ok" and out["pages"] == 2 and out["records"] == 2


def test_max_pages_is_partial_and_keeps_records(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    s = _sync(test_client, "list_tickets", paging={"size": 2})
    with respx.mock() as mock:
        mock.get(f"{BASE}/tickets").mock(side_effect=_tickets_api(TICKETS))
        _run_sync(test_client, s["id"])
    test_client.patch(f"/api/connections/desk/syncs/{s['id']}", json={"max_pages": 1})
    with respx.mock() as mock:
        mock.get(f"{BASE}/tickets").mock(side_effect=_tickets_api(TICKETS))
        out = _run_sync(test_client, s["id"])
    assert out["status"] == "partial" and out["removed"] == 0 and "max_pages" in out["note"]
    assert test_client.get("/api/connections/desk/syncs").json()[0]["record_count"] == 5


def test_api_error_keeps_the_old_copy(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    s = _sync(test_client, "list_tickets", paging={"size": 10})
    with respx.mock() as mock:
        mock.get(f"{BASE}/tickets").mock(side_effect=_tickets_api(TICKETS))
        _run_sync(test_client, s["id"])
    with respx.mock() as mock:
        mock.get(f"{BASE}/tickets").mock(return_value=httpx.Response(503, json={"message": "maintenance"}))
        out = _run_sync(test_client, s["id"])
    assert out["status"] == "error" and "503" in out["error"]
    listed = test_client.get("/api/connections/desk/syncs").json()[0]
    assert listed["status"] == "error" and listed["record_count"] == 5 and "503" in listed["last_error"]


def test_records_path_and_key_field(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    s = _sync(test_client, "list_devices", records_path="result.machines", key_field="serial",
              paging={"type": "none"})
    with respx.mock() as mock:
        mock.get(f"{BASE}/devices").mock(return_value=httpx.Response(200, json={
            "result": {"machines": [{"serial": "S-1", "host": "a"}, {"serial": "S-2", "host": "b"}]}}))
        out = _run_sync(test_client, s["id"])
    assert out["records"] == 2 and out["pages"] == 1
    assert test_client.get("/api/connections/desk/records/S-2").json()["data"]["host"] == "b"

    test_client.patch(f"/api/connections/desk/syncs/{s['id']}", json={"records_path": "nope"})
    with respx.mock() as mock:
        mock.get(f"{BASE}/devices").mock(return_value=httpx.Response(200, json={"result": {}}))
        out = _run_sync(test_client, s["id"])
    assert out["status"] == "error" and "records_path" in out["error"]


# ── rules ─────────────────────────────────────────────────────────────────


def test_only_reads_and_only_approvers(app_client, db_factory) -> None:  # type: ignore[no-untyped-def]
    app_client.cookies.set("msp_session", make_session(db_factory, ADMIN_ROLES, "admin@example.com"))
    _setup(app_client)
    r = app_client.post("/api/connections/desk/syncs", json={"operation": "create_ticket"})
    assert r.status_code == 400 and "read operations" in r.text
    assert app_client.post("/api/connections/desk/syncs", json={"operation": "nope"}).status_code == 404
    bad = app_client.post("/api/connections/desk/syncs", json={"operation": "list_tickets", "args": {"page": "x"}})
    assert bad.status_code == 400  # args checked against the operation's schema
    s = _sync(app_client, "list_tickets")
    assert app_client.post("/api/connections/desk/syncs", json={"operation": "list_tickets"}).status_code == 409

    app_client.cookies.set("msp_session", make_session(db_factory, EDITOR, "ed@example.com"))
    assert app_client.post("/api/connections/desk/syncs", json={"operation": "list_devices"}).status_code == 403
    assert app_client.post(f"/api/connections/desk/syncs/{s['id']}/run").status_code == 403
    assert app_client.delete(f"/api/connections/desk/syncs/{s['id']}").status_code == 403
    assert app_client.get("/api/connections/desk/syncs").status_code == 200


def test_scopes_protect_stored_records(app_client, db_factory) -> None:  # type: ignore[no-untyped-def]
    app_client.cookies.set("msp_session", make_session(db_factory, ADMIN_ROLES, "admin@example.com"))
    _setup(app_client)
    s = _sync(app_client, "list_tickets", paging={"size": 10})
    with respx.mock() as mock:
        mock.get(f"{BASE}/tickets").mock(side_effect=_tickets_api(TICKETS))
        _run_sync(app_client, s["id"])
    good = app_client.post("/api/admin/api-keys", json={"name": "a", "scopes": ["desk:read"]}).json()["key"]
    other = app_client.post("/api/admin/api-keys", json={"name": "b", "scopes": ["github:read"]}).json()["key"]
    app_client.cookies.clear()

    assert app_client.get("/api/connections/desk/records?q=ticket", headers={"X-API-Key": good}).json()["count"]
    for path in ("/api/connections/desk/records", "/api/connections/desk/records/1", "/api/connections/desk/syncs"):
        assert app_client.get(path, headers={"X-API-Key": other}).status_code == 403, path
    assert app_client.get("/api/search/records?q=ticket", headers={"X-API-Key": other}).json()["count"] == 0
    assert app_client.get("/api/search/records?q=ticket", headers={"X-API-Key": good}).json()["count"] > 0


# ── searching ─────────────────────────────────────────────────────────────


def test_search_and_filters(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    s = _sync(test_client, "list_tickets", paging={"size": 10})
    with respx.mock() as mock:
        mock.get(f"{BASE}/tickets").mock(side_effect=_tickets_api(TICKETS))
        _run_sync(test_client, s["id"])

    hits = test_client.get("/api/connections/desk/records?q=globex").json()["records"]
    assert {h["data"]["company"]["name"] for h in hits} == {"Globex"}
    assert test_client.get("/api/connections/desk/records?q=globx").json()["count"] > 0       # typo
    open_acme = test_client.get("/api/connections/desk/records?f.status=open&f.company.name=Acme").json()
    assert [r["key"] for r in open_acme["records"]] == ["3"] and open_acme["total"] == 1
    any_of = test_client.get("/api/connections/desk/records?f.id=1&f.id=2").json()
    assert any_of["total"] == 2
    exact = test_client.get("/api/connections/desk/records?q=4").json()["records"]
    assert exact[0]["key"] == "4"                                                              # key match first
    assert test_client.get("/api/connections/nothing/records").status_code in (403, 404)
    everywhere = test_client.get("/api/search/records?q=initech").json()
    assert everywhere["count"] and all(r["connection"] == "desk" for r in everywhere["records"])


def test_sql_source_kept_locally(test_client, tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "desk.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE tickets (id INTEGER PRIMARY KEY, summary TEXT, status TEXT);"
                      "INSERT INTO tickets VALUES (1,'VPN down','open'),(2,'Printer jam','closed'),(3,'Laptop','open');")
    con.commit()
    con.close()
    r = test_client.post("/api/connections", json={"kind": "sql", "name": "Desk DB",
                                                   "auth": {"dsn": f"sqlite:///{db.as_posix()}"}})
    assert r.status_code == 200, r.text
    s = test_client.post("/api/connections/desk_db/syncs?run=false", json={
        "operation": "run_query", "args": {"sql": "SELECT id, summary, status FROM tickets"}}).json()
    out = _run_sync(test_client, s["id"], "desk_db")
    assert out["status"] == "ok" and out["records"] == 3
    hits = test_client.get("/api/connections/desk_db/records?q=vpn").json()["records"]
    assert hits[0]["key"] == "1" and hits[0]["data"]["summary"] == "VPN down"


# ── MCP ───────────────────────────────────────────────────────────────────


def _tool(client, path: str, name: str, args: dict[str, Any]) -> tuple[bool, Any]:  # type: ignore[no-untyped-def]
    r = client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": args}}).json()["result"]
    text = r["content"][0]["text"]
    try:
        return not r["isError"], json.loads(text)
    except ValueError:
        return not r["isError"], text


def test_mcp_record_tools(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    tools = {t["name"] for t in test_client.post("/mcp/desk", json={"jsonrpc": "2.0", "id": 1,
                                                                     "method": "tools/list"}).json()["result"]["tools"]}
    assert "search_records" not in tools  # nothing kept yet

    s = _sync(test_client, "list_tickets", paging={"size": 10})
    with respx.mock() as mock:
        mock.get(f"{BASE}/tickets").mock(side_effect=_tickets_api(TICKETS))
        _run_sync(test_client, s["id"])

    init = test_client.post("/mcp/desk", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                               "params": {}}).json()["result"]
    assert "Local copy" in init["instructions"] and "list_tickets: 5 records" in init["instructions"]
    tools = {t["name"]: t for t in test_client.post("/mcp/desk", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]}
    assert tools["search_records"]["inputSchema"]["properties"]["operation"]["enum"] == ["list_tickets"]

    ok, res = _tool(test_client, "/mcp/desk", "search_records", {"query": "acme", "filters": {"status": "open"}})
    assert ok and [r["key"] for r in res["records"]] == ["3"]
    ok, rec = _tool(test_client, "/mcp/desk", "get_record", {"key": "2"})
    assert ok and rec["data"]["summary"] == "Ticket 2"
    ok, err = _tool(test_client, "/mcp/desk", "search_records", {})
    assert not ok and "query" in err

    ok, listed = _tool(test_client, "/mcp", "list_connections", {})
    desk = next(c for c in listed["connections"] if c["connection"] == "desk")
    assert desk["local_copy"][0]["records"] == 5
    ok, res = _tool(test_client, "/mcp", "search_records", {"query": "initech"})
    assert ok and res["records"] and res["records"][0]["connection"] == "desk"
    ok, rec = _tool(test_client, "/mcp", "get_record", {"connection": "desk", "key": "1"})
    assert ok and rec["key"] == "1"


# ── scheduling + clean-up ─────────────────────────────────────────────────


def test_run_due_and_scheduler_off_in_tests(test_client) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime, timedelta

    from app.connections.sync import run_due, start_sync_loop

    assert start_sync_loop() is None  # MSP_CONNECTION_SYNC=off in tests
    _setup(test_client)
    s = _sync(test_client, "list_tickets", paging={"size": 10}, interval_minutes=60)
    paused = _sync(test_client, "list_devices")
    test_client.patch(f"/api/connections/desk/syncs/{paused['id']}", json={"enabled": False})
    with respx.mock() as mock:
        mock.get(f"{BASE}/tickets").mock(side_effect=_tickets_api(TICKETS))
        first = _run(run_due())
        again = _run(run_due())
        later = _run(run_due(datetime.now(UTC) + timedelta(minutes=61)))
    assert [r["sync"] for r in first] == [s["id"]]   # never run → due; the paused one is skipped
    assert again == []                               # not due yet
    assert [r["status"] for r in later] == ["ok"]


def test_deleting_removes_the_local_copy(test_client, db_factory) -> None:  # type: ignore[no-untyped-def]
    from app.db.schema import AuditLog, ConnectionRecord, ConnectionSync

    _setup(test_client)
    s = _sync(test_client, "list_tickets", paging={"size": 10})
    with respx.mock() as mock:
        mock.get(f"{BASE}/tickets").mock(side_effect=_tickets_api(TICKETS))
        _run_sync(test_client, s["id"])

    async def counts() -> tuple[int, int]:
        async with db_factory() as db:
            return (len((await db.execute(select(ConnectionSync))).scalars().all()),
                    len((await db.execute(select(ConnectionRecord))).scalars().all()))

    async def audit_rows() -> list[AuditLog]:
        async with db_factory() as db:
            return list((await db.execute(select(AuditLog).where(AuditLog.source == "sync"))).scalars())

    rows = _run(audit_rows())
    assert len(rows) == 1 and rows[0].action == "desk.list_tickets"  # one audit entry per run, not per page
    assert _run(counts()) == (1, 5)
    out = test_client.delete(f"/api/connections/desk/syncs/{s['id']}").json()
    assert out["records_deleted"] == 5 and _run(counts()) == (0, 0)

    s = _sync(test_client, "list_tickets", paging={"size": 10})
    with respx.mock() as mock:
        mock.get(f"{BASE}/tickets").mock(side_effect=_tickets_api(TICKETS))
        _run_sync(test_client, s["id"])
    assert test_client.delete("/api/connections/desk").status_code == 200
    assert _run(counts()) == (0, 0)


def test_extract_records_shapes() -> None:
    from app.connections.sync import extract_records, record_key

    assert len(extract_records({"status": 200, "data": {"value": [{"ID": 1}, {"ID": 2}]}})) == 2      # OData
    assert len(extract_records({"status": 200, "data": {"d": {"results": [{"a": 1}]}}})) == 1         # OData v2
    gql = {"status": 200, "data": {"data": {"tickets": {"edges": [{"node": {"id": "t1"}}],
                                                         "pageInfo": {"hasNextPage": False}}}}}
    assert extract_records(gql) == [{"id": "t1"}]
    assert extract_records({"rows": [{"id": 1}], "row_count": 1, "truncated": False}) == [{"id": 1}]   # SQL
    assert extract_records({"documents": [{"_id": "x"}]}) == [{"_id": "x"}]                           # MongoDB
    assert extract_records({"status": 200, "data": ["a", "b"]}) == [{"value": "a"}, {"value": "b"}]
    assert extract_records({"status": 200, "data": {"id": 9, "name": "single"}}) == [{"id": 9, "name": "single"}]
    assert record_key({"Id": 5}) == "5" and record_key({"_id": "abc"}) == "abc"
    assert record_key({"name": "x"}).startswith("h:")                                                  # no key
    assert record_key({"a": {"b": 7}}, "a.b") == "7"
