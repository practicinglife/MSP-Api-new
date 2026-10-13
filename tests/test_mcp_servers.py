"""Phase 4 — each connection is its own MCP server, plus groups and a hub.

Everything is local: definitions come from tests/fixtures/connections and the APIs behind them are mocked with
respx. conftest blocks real network.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.conftest import ADMIN_ROLES, make_session

FIX = Path(__file__).parent / "fixtures" / "connections"
VIEWER = ["MSPTools.User"]
PETS = "https://api.petstore.example.com/v1"


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _setup(client) -> None:  # type: ignore[no-untyped-def]
    """Petstore (OpenAPI, one pinned operation) and Helpdesk (Postman)."""
    for body in ({"kind": "openapi", "name": "Petstore", "auth": {"type": "api_key", "api_key": "k-123"},
                  "source": {"text": (FIX / "petstore.yaml").read_text(encoding="utf-8")}},
                 {"kind": "postman", "name": "Helpdesk",
                  "source": {"text": (FIX / "collection.postman.json").read_text(encoding="utf-8")}}):
        assert client.post("/api/connections", json=body).status_code == 200
    assert client.patch("/api/connections/petstore/operations/show_pet_by_id", json={"pinned": True}).status_code == 200


def rpc(client, path: str, method: str, params: dict[str, Any] | None = None, headers=None, mid: int = 1):  # type: ignore[no-untyped-def]
    msg = {"jsonrpc": "2.0", "id": mid, "method": method, **({"params": params} if params is not None else {})}
    return client.post(path, json=msg, headers=headers or {})


def tool(client, path: str, name: str, args: dict[str, Any] | None = None, headers=None) -> tuple[bool, Any]:  # type: ignore[no-untyped-def]
    r = rpc(client, path, "tools/call", {"name": name, "arguments": args or {}}, headers)
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    text = res["content"][0]["text"]
    try:
        data = json.loads(text)
    except ValueError:
        data = text
    return not res["isError"], data


def _key(client, factory, scopes: list[str], role: str = "user") -> str:  # type: ignore[no-untyped-def]
    client.cookies.set("msp_session", make_session(factory, ADMIN_ROLES, "admin@example.com"))
    r = client.post("/api/admin/api-keys", json={"name": "k", "role": role, "scopes": scopes})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return r.json()["key"]


# ── /mcp/{slug} ───────────────────────────────────────────────────────────


def test_connection_server_handshake_and_tools(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    init = rpc(test_client, "/mcp/petstore", "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
    assert init["serverInfo"]["name"] == "petstore" and init["protocolVersion"] == "2025-06-18"
    assert "search_operations" in init["instructions"] and "read-only" in init["instructions"]
    assert "resources" in init["capabilities"]

    tools = {t["name"]: t for t in rpc(test_client, "/mcp/petstore", "tools/list").json()["result"]["tools"]}
    assert set(tools) == {"search_operations", "describe_operation", "call_operation", "show_pet_by_id"}
    typed = tools["show_pet_by_id"]
    assert typed["inputSchema"]["required"] == ["petId"] and typed["annotations"]["readOnlyHint"] is True
    assert tools["call_operation"]["inputSchema"]["required"] == ["operation"]

    ok, found = tool(test_client, "/mcp/petstore", "search_operations", {"query": "find a pet by its id"})
    assert ok and found["operations"][0]["operation"] == "show_pet_by_id"
    assert all("connection" not in o for o in found["operations"])  # one connection: no need to say which
    ok, desc = tool(test_client, "/mcp/petstore", "describe_operation", {"operation": "create_pet"})
    assert ok and desc["input_schema"]["properties"]["body"] and "read-only" in desc["write_mode"]

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{PETS}/pets/7").mock(return_value=httpx.Response(200, json={"id": 7, "name": "Rex"}))
        ok, out = tool(test_client, "/mcp/petstore", "show_pet_by_id", {"petId": 7})
        assert ok and out["result"]["data"] == {"id": 7, "name": "Rex"}
        ok, out = tool(test_client, "/mcp/petstore", "call_operation", {"operation": "show_pet_by_id", "args": {"petId": 7}})
        assert ok and out["result"]["data"]["name"] == "Rex"
    assert route.calls.last.request.headers["X-API-Key"] == "k-123"

    ok, err = tool(test_client, "/mcp/petstore", "show_pet_by_id", {"petId": "seven"})  # schema-checked
    assert not ok and "Invalid arguments" in err["error"]
    ok, err = tool(test_client, "/mcp/petstore", "call_operation", {"operation": "create_pet", "args": {"body": {"name": "x"}}})
    assert not ok and "read-only" in err["error"]  # default write mode
    ok, err = tool(test_client, "/mcp/petstore", "search_operations", {})
    assert not ok and "query" in err
    with respx.mock() as mock:
        mock.get(f"{PETS}/pets/9").mock(return_value=httpx.Response(404, json={"message": "no such pet"}))
        ok, err = tool(test_client, "/mcp/petstore", "show_pet_by_id", {"petId": 9})
    assert not ok and err["upstream_status"] == 404

    unknown = rpc(test_client, "/mcp/petstore", "tools/call", {"name": "list_pets", "arguments": {}}).json()
    assert unknown["error"]["code"] == -32602  # not pinned → not a tool of its own

    res = rpc(test_client, "/mcp/petstore", "resources/list").json()["result"]["resources"]
    assert res[0]["uri"] == "api://petstore/operations"
    content = rpc(test_client, "/mcp/petstore", "resources/read", {"uri": res[0]["uri"]}).json()["result"]["contents"][0]
    assert {o["name"] for o in json.loads(content["text"])} == {"list_pets", "create_pet", "show_pet_by_id", "delete_pet"}


def test_protocol_details(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    note = test_client.post("/mcp/petstore", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202
    batch = test_client.post("/mcp/petstore", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                                    {"jsonrpc": "2.0", "id": 2, "method": "nope"}]).json()
    assert batch[0]["result"] == {} and batch[1]["error"]["code"] == -32601
    old = rpc(test_client, "/mcp/petstore", "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
    assert old["protocolVersion"] == "2025-06-18"
    assert test_client.get("/mcp/petstore").status_code == 405
    assert test_client.post("/mcp/petstore", content=b"{", headers={"Content-Type": "application/json"}).status_code == 400
    plain = test_client.post("/mcp/petstore", content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                             headers={"Content-Type": "text/plain"})
    assert plain.status_code == 415  # a cross-site form can't fake JSON


def test_writes_follow_the_write_mode(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    assert test_client.put("/api/admin/write-modes/petstore", json={"write_mode": "approval"}).status_code == 200
    ok, out = tool(test_client, "/mcp/petstore", "call_operation", {"operation": "create_pet", "args": {"body": {"name": "Max"}}})
    assert ok and out["pending_approval"] is True and out["change"]["id"]
    init = rpc(test_client, "/mcp/petstore", "initialize", {}).json()["result"]
    assert "change requests" in init["instructions"]

    assert test_client.put("/api/admin/write-modes/petstore", json={"write_mode": "direct"}).status_code == 200
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{PETS}/pets").mock(return_value=httpx.Response(201, json={"id": 3}))
        ok, out = tool(test_client, "/mcp/petstore", "call_operation", {"operation": "create_pet", "args": {"body": {"name": "Rex"}}})
    assert ok and out["result"]["data"] == {"id": 3}
    assert json.loads(route.calls.last.request.content) == {"name": "Rex"}


def test_unknown_and_disabled_connections(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    assert rpc(test_client, "/mcp/nothing_here", "ping").status_code == 404
    assert test_client.patch("/api/connections/petstore", json={"enabled": False}).status_code == 200
    assert rpc(test_client, "/mcp/petstore", "ping").status_code == 404
    ok, listed = tool(test_client, "/mcp", "list_connections")
    assert [c["connection"] for c in listed["connections"]] == ["helpdesk"]


# ── groups ────────────────────────────────────────────────────────────────


def test_groups_publish_a_hand_picked_set(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    r = test_client.patch("/api/connections/petstore",
                          json={"settings": {"mcp_groups": {"pets-read": ["list_pets", "show_pet_by_id"]}}})
    assert r.status_code == 200, r.text
    tools = {t["name"] for t in rpc(test_client, "/mcp/petstore/pets-read", "tools/list").json()["result"]["tools"]}
    assert tools == {"search_operations", "describe_operation", "call_operation", "list_pets", "show_pet_by_id"}
    ok, found = tool(test_client, "/mcp/petstore/pets-read", "search_operations", {"query": "pet"})
    assert ok and {o["operation"] for o in found["operations"]} <= {"list_pets", "show_pet_by_id"}
    ok, err = tool(test_client, "/mcp/petstore/pets-read", "call_operation", {"operation": "delete_pet", "args": {"petId": 1}})
    assert not ok and "not part of this server" in err
    ok, err = tool(test_client, "/mcp/petstore/pets-read", "describe_operation", {"operation": "create_pet"})
    assert not ok
    assert rpc(test_client, "/mcp/petstore/nope", "ping").status_code == 404

    servers = test_client.get("/api/mcp/servers").json()
    pet = next(s for s in servers["servers"] if s["connection"] == "petstore")
    assert pet["groups"][0]["url"].endswith("/mcp/petstore/pets-read")


# ── hub ───────────────────────────────────────────────────────────────────


def test_hub_searches_and_calls_every_connection(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    tools = {t["name"] for t in rpc(test_client, "/mcp", "tools/list").json()["result"]["tools"]}
    assert tools == {"list_connections", "search_operations", "describe_operation", "call_operation"}
    init = rpc(test_client, "/mcp", "initialize", {}).json()["result"]
    assert "helpdesk" in init["instructions"] and "petstore" in init["instructions"]

    ok, listed = tool(test_client, "/mcp", "list_connections")
    by_slug = {c["connection"]: c for c in listed["connections"]}
    assert set(by_slug) == {"petstore", "helpdesk"} and by_slug["petstore"]["mcp_url"].endswith("/mcp/petstore")
    assert by_slug["petstore"]["write_mode"] == "read_only"

    ok, found = tool(test_client, "/mcp", "search_operations", {"query": "open tickets"})
    assert ok and found["operations"][0]["connection"] == "helpdesk"
    ok, desc = tool(test_client, "/mcp", "describe_operation", {"connection": "helpdesk", "operation": "get_ticket"})
    assert ok and desc["name"] == "get_ticket"
    ok, err = tool(test_client, "/mcp", "call_operation", {"operation": "get_ticket"})
    assert not ok and "connection" in err  # the hub needs to know which connection

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://helpdesk.example.com/api/tickets/1001").mock(return_value=httpx.Response(200, json={"id": 1001}))
        ok, out = tool(test_client, "/mcp", "call_operation",
                       {"connection": "helpdesk", "operation": "get_ticket", "args": {"id": "1001"}})
    assert ok and out["result"]["data"] == {"id": 1001}


# ── permissions ───────────────────────────────────────────────────────────


def test_scopes_limit_servers_and_hub(app_client, db_factory) -> None:  # type: ignore[no-untyped-def]
    app_client.cookies.set("msp_session", make_session(db_factory, ADMIN_ROLES, "admin@example.com"))
    _setup(app_client)
    app_client.cookies.clear()
    key = _key(app_client, db_factory, ["petstore:read"])
    h = {"X-API-Key": key}

    assert rpc(app_client, "/mcp/petstore", "ping", headers=h).status_code == 200
    denied = rpc(app_client, "/mcp/helpdesk", "ping", headers=h)
    assert denied.status_code == 403 and "may not use" in denied.text
    ok, listed = tool(app_client, "/mcp", "list_connections", headers=h)
    assert [c["connection"] for c in listed["connections"]] == ["petstore"]
    ok, found = tool(app_client, "/mcp", "search_operations", {"query": "open tickets"}, headers=h)
    assert all(o["connection"] == "petstore" for o in found["operations"])
    ok, err = tool(app_client, "/mcp", "call_operation", {"connection": "helpdesk", "operation": "get_ticket",
                                                          "args": {"id": "1"}}, headers=h)
    assert not ok and "may not use" in err["error"]
    ok, err = tool(app_client, "/mcp", "describe_operation", {"connection": "helpdesk", "operation": "get_ticket"}, headers=h)
    assert not ok

    servers = app_client.get("/api/mcp/servers", headers=h).json()
    assert [s["connection"] for s in servers["servers"]] == ["petstore"]


def test_unauthenticated_mcp_points_at_its_own_oauth_metadata(app_client, db_factory) -> None:  # type: ignore[no-untyped-def]
    for path in ("/mcp", "/mcp/petstore", "/mcp/petstore/pets-read"):
        r = app_client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401
        assert f'oauth-protected-resource{path}"' in r.headers["www-authenticate"]
    browser = app_client.get("/mcp/petstore", headers={"Accept": "text/html"}, follow_redirects=False)
    assert browser.status_code == 401  # MCP clients get 401 + metadata, never a login redirect

    from app.org_profile import external_base_url

    EXTERNAL_BASE = external_base_url()

    meta = app_client.get("/.well-known/oauth-protected-resource/mcp/petstore").json()
    assert meta["resource"] == f"{EXTERNAL_BASE}/mcp/petstore" and meta["authorization_servers"] == [EXTERNAL_BASE]
    assert app_client.get("/.well-known/oauth-protected-resource/mcp").json()["resource"] == f"{EXTERNAL_BASE}/mcp"
    assert app_client.get("/.well-known/oauth-protected-resource/msp/mcp").status_code == 200
    assert app_client.get("/.well-known/oauth-protected-resource/api/companies").status_code == 404


def _token(factory, resource: str) -> str:  # type: ignore[no-untyped-def]
    from app.db.schema import OAuthToken

    token = secrets.token_urlsafe(32)

    async def _add() -> None:
        async with factory() as db:
            db.add(OAuthToken(token_hash=hashlib.sha256(token.encode()).hexdigest(), kind="access",
                              client_id="c_test", api_key_id="m365:oid-1", user_upn="sso@example.com",
                              user_roles=VIEWER, resource=resource,
                              expires_at=datetime.now(UTC) + timedelta(hours=1), created_at=datetime.now(UTC)))
            await db.commit()

    _run(_add())
    return token


def test_oauth_token_is_bound_to_one_server(app_client, db_factory) -> None:  # type: ignore[no-untyped-def]
    from app.api.external_auth import resource_allowed
    from app.org_profile import external_base_url

    EXTERNAL_BASE = external_base_url()

    app_client.cookies.set("msp_session", make_session(db_factory, ADMIN_ROLES, "admin@example.com"))
    _setup(app_client)
    app_client.cookies.clear()
    h = {"Authorization": f"Bearer {_token(db_factory, f'{EXTERNAL_BASE}/mcp/petstore')}"}
    assert rpc(app_client, "/mcp/petstore", "ping", headers=h).status_code == 200
    for other in ("/mcp/helpdesk", "/mcp", "/msp/mcp", "/mcp/petstore/pets-read"):
        assert rpc(app_client, other, "ping", headers=h).status_code == 403, other
    assert app_client.get("/api/connections", headers=h).status_code == 403

    hub = {"Authorization": f"Bearer {_token(db_factory, f'{EXTERNAL_BASE}/mcp')}"}
    ok, listed = tool(app_client, "/mcp", "list_connections", headers=hub)
    assert ok and len(listed["connections"]) == 2
    assert rpc(app_client, "/mcp/petstore", "ping", headers=hub).status_code == 403

    for good in ("/mcp", "/mcp/petstore", "/mcp/petstore/pets-read", "/msp/mcp"):
        assert resource_allowed(f"{EXTERNAL_BASE}{good}"), good
    for bad in ("/mcp/../api", "/api/connections", "/mcp/Petstore", "/mcp/a/b/c", "/mcp/petstore?x=1"):
        assert not resource_allowed(f"{EXTERNAL_BASE}{bad}"), bad
    assert not resource_allowed("https://evil.example.com/mcp/petstore")


def test_oauth_consent_page_names_the_server(app_client, db_factory) -> None:  # type: ignore[no-untyped-def]
    from app.org_profile import external_base_url

    EXTERNAL_BASE = external_base_url()

    reg = app_client.post("/oauth/register", json={"redirect_uris": ["http://localhost:3333/cb"]}).json()
    params = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": "http://localhost:3333/cb",
              "code_challenge": "x" * 43, "code_challenge_method": "S256", "resource": f"{EXTERNAL_BASE}/mcp/petstore"}
    page = app_client.get("/oauth/authorize", params=params)
    assert page.status_code == 200 and "/mcp/petstore" in page.text


def test_session_mcp_calls_refuse_other_sites(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    r = rpc(test_client, "/mcp/petstore", "ping", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403
    assert rpc(test_client, "/mcp/petstore", "ping", headers={"Origin": "http://testserver"}).status_code == 200


def test_mcp_tool_calls_are_audited(test_client, db_factory) -> None:  # type: ignore[no-untyped-def]
    from app.db.schema import AuditLog

    _setup(test_client)
    tool(test_client, "/mcp/petstore", "search_operations", {"query": "pets"})
    with respx.mock() as mock:
        mock.get(f"{PETS}/pets/3").mock(return_value=httpx.Response(200, json={}))
        tool(test_client, "/mcp/petstore", "show_pet_by_id", {"petId": 3})
    tool(test_client, "/mcp", "list_connections")

    async def _rows() -> list[AuditLog]:
        async with db_factory() as db:
            return list((await db.execute(select(AuditLog))).scalars())

    rows = {(r.action, r.status, r.source) for r in _run(_rows())}
    assert ("petstore:search_operations", 200, "mcp") in rows
    assert ("hub:list_connections", 200, "mcp") in rows
    assert any(a == "petstore.show_pet_by_id" and s == 200 for a, s, _ in rows)


# ── client setup + stdio bridge ───────────────────────────────────────────


def test_server_list_has_client_configs(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    out = test_client.get("/api/mcp/servers").json()
    assert out["hub"]["url"].endswith("/mcp")
    pet = next(s for s in out["servers"] if s["connection"] == "petstore")
    cfg = pet["configs"]
    assert cfg["claude_code"].startswith("claude mcp add --transport http petstore ") and "X-API-Key" in cfg["claude_code"]
    assert cfg["vscode"]["servers"]["petstore"]["url"] == pet["url"]
    assert cfg["claude_desktop"]["mcpServers"]["petstore"]["args"][1] == pet["url"]
    assert "msp_" not in json.dumps(out)  # never a real key


def test_bridge_script_is_public_and_secret_free(app_client) -> None:  # type: ignore[no-untyped-def]
    r = app_client.get("/mcp/bridge.py")
    assert r.status_code == 200 and "X-API-Key" in r.text and "msp_" not in r.text


def test_bridge_url_check() -> None:
    from app.mcp import stdio_bridge

    assert stdio_bridge._check_url("https://tools.example.com/mcp/x")
    assert stdio_bridge._check_url("http://127.0.0.1:8000/mcp/x")
    assert stdio_bridge._check_url("http://192.168.1.20/mcp")
    with pytest.raises(SystemExit):
        stdio_bridge._check_url("http://8.8.8.8/mcp")  # public address over plain http (IP literal: no lookup)
    with pytest.raises(SystemExit):
        stdio_bridge._check_url("ftp://tools.example.com/mcp")


def test_bridge_end_to_end(app_client, db_factory, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """The stdio bridge forwards each line to the server with the key, and turns HTTP errors into JSON-RPC."""
    import sys

    from app.mcp import stdio_bridge

    app_client.cookies.set("msp_session", make_session(db_factory, ADMIN_ROLES, "admin@example.com"))
    _setup(app_client)
    app_client.cookies.clear()
    key = _key(app_client, db_factory, ["petstore:read"])

    class _Resp:
        def __init__(self, r: httpx.Response) -> None:
            self.status, self.headers, self._body = r.status_code, r.headers, r.content

        def read(self) -> bytes:
            return self._body

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *a: Any) -> None:
            pass

    def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
        path = req.full_url.replace("https://tools.example.com", "")
        r = app_client.post(path, content=req.data, headers=dict(req.header_items()))
        if r.status_code >= 400:
            raise stdio_bridge.urllib.error.HTTPError(req.full_url, r.status_code, "err", r.headers, io.BytesIO(r.content))
        return _Resp(r)

    def run(url: str, lines: list[dict[str, Any]], key_value: str) -> list[dict[str, Any]]:
        monkeypatch.setenv("MSP_API_KEY", key_value)
        monkeypatch.setattr(stdio_bridge.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(sys, "argv", ["mcp-bridge.py", url])
        stdin = type("S", (), {"buffer": io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in lines))})()
        monkeypatch.setattr(sys, "stdin", stdin)
        capsys.readouterr()
        stdio_bridge.main()
        return [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip()]

    out = run("https://tools.example.com/mcp/petstore",
              [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
               {"jsonrpc": "2.0", "method": "notifications/initialized"},
               {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}], key)
    assert [m["id"] for m in out] == [1, 2]  # the notification got no reply
    assert out[0]["result"]["serverInfo"]["name"] == "petstore"
    assert any(t["name"] == "show_pet_by_id" for t in out[1]["result"]["tools"])

    denied = run("https://tools.example.com/mcp/helpdesk", [{"jsonrpc": "2.0", "id": 5, "method": "ping"}], key)
    assert denied[0]["id"] == 5 and "403" in denied[0]["error"]["message"]
    bad_key = run("https://tools.example.com/mcp/petstore", [{"jsonrpc": "2.0", "id": 6, "method": "ping"}], "msp_wrong")
    assert "401" in bad_key[0]["error"]["message"] and "connection key" in bad_key[0]["error"]["message"]
