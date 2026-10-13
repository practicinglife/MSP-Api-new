"""Phase 2 — import any API and call its operations.

Everything is local: definitions come from tests/fixtures/connections, remote APIs are mocked with respx,
the SQL database is a temporary SQLite file and MongoDB is a fake client. conftest blocks real network.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from tests.conftest import ADMIN_ROLES, make_session

FIX = Path(__file__).parent / "fixtures" / "connections"
EDITOR = ["MSPTools.User", "MSPTools.Editor"]
VIEWER = ["MSPTools.User"]


def _text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _create(client, **body) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    r = client.post("/api/connections", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _call(client, slug: str, op: str, **args):  # type: ignore[no-untyped-def]
    return client.post(f"/api/connections/{slug}/operations/{op}/call", json={"args": args})


def _mode(client, slug: str, mode: str) -> None:  # type: ignore[no-untyped-def]
    assert client.put(f"/api/admin/write-modes/{slug}", json={"write_mode": mode}).status_code == 200


# ── OpenAPI ───────────────────────────────────────────────────────────────


def test_openapi3_parsing() -> None:
    from app.connections.adapters.openapi import (
        _auth_hint,
        _base_url,
        build_operations,
        parse_document,
    )

    spec = parse_document(_text("petstore.yaml"))
    ops, refs = build_operations(spec)
    by_name = {o.name: o for o in ops}
    assert set(by_name) == {"list_pets", "create_pet", "show_pet_by_id", "delete_pet"}
    show = by_name["show_pet_by_id"]
    assert show.input_schema["required"] == ["petId"]
    assert show.input_schema["properties"]["petId"]["type"] == "integer"
    assert show.read_only and not by_name["create_pet"].read_only
    assert by_name["delete_pet"].deprecated
    body = by_name["create_pet"].input_schema["properties"]["body"]
    assert body["required"] == ["name"] and body["properties"]["tag"]["type"] == ["string", "null"]
    assert "nested object" in body["properties"]["parent"]["description"]  # Pet → parent: Pet cycle stopped
    assert "X-Request-Id" in by_name["list_pets"].input_schema["properties"]
    assert _base_url(spec, "") == "https://api.petstore.example.com/v1"
    assert _auth_hint(spec, refs) == {"auth_type": "api_key", "api_key_name": "X-API-Key", "api_key_in": "header",
                                      "scheme": "ApiKeyAuth"}


def test_swagger2_parsing() -> None:
    from app.connections.adapters.openapi import (
        _auth_hint,
        _base_url,
        build_operations,
        parse_document,
    )

    spec = parse_document(_text("swagger2.json"))
    ops, refs = build_operations(spec)
    by_name = {o.name: o for o in ops}
    assert set(by_name) == {"get_users_id", "update_user", "upload_avatar"}
    assert by_name["update_user"].binding["body"] == {"arg": "body", "content_type": "application/json"}
    assert "body" in by_name["update_user"].input_schema["required"]
    upload = by_name["upload_avatar"]
    assert upload.binding["body"]["content_type"] == "multipart/form-data"
    assert upload.input_schema["properties"]["body"]["properties"]["file"]["format"] == "binary"
    assert _base_url(spec, "") == "https://users.example.com/v2"
    assert _auth_hint(spec, refs)["auth_type"] == "basic"


def test_openapi_import_and_calls(test_client) -> None:  # type: ignore[no-untyped-def]
    out = _create(test_client, kind="openapi", name="Petstore", source={"text": _text("petstore.yaml")},
                  auth={"type": "api_key", "api_key": "k-123"})
    assert out["slug"] == "petstore" and out["operations"] == 4 and out["auth_type"] == "api_key"

    ops = test_client.get("/api/connections/petstore/operations?q=pet").json()
    assert ops["total"] == 4
    detail = test_client.get("/api/connections/petstore/operations/show_pet_by_id").json()
    assert detail["input_schema"]["required"] == ["petId"]

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("https://api.petstore.example.com/v1/pets/7").mock(
            return_value=httpx.Response(200, json={"id": 7, "name": "Rex"}))
        r = _call(test_client, "petstore", "show_pet_by_id", petId=7)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["data"] == {"id": 7, "name": "Rex"}
    assert route.calls.last.request.headers["X-API-Key"] == "k-123"

    assert _call(test_client, "petstore", "show_pet_by_id", petId="abc").status_code == 400  # schema-checked
    assert _call(test_client, "petstore", "show_pet_by_id").status_code == 400                 # required

    with respx.mock() as mock:
        mock.get("https://api.petstore.example.com/v1/pets").mock(return_value=httpx.Response(401, json={"e": "bad key"}))
        r = _call(test_client, "petstore", "list_pets", limit=5)
    assert r.status_code == 502 and r.json()["upstream_status"] == 401  # never mistaken for "sign in here"


def test_write_modes_on_connections(test_client) -> None:  # type: ignore[no-untyped-def]
    _create(test_client, kind="openapi", name="Petstore", source={"text": _text("petstore.yaml")},
            auth={"type": "api_key", "api_key": "k-123"})
    r = _call(test_client, "petstore", "create_pet", body={"name": "Rex"})
    assert r.status_code == 403 and "read-only" in r.text  # default

    _mode(test_client, "petstore", "direct")
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://api.petstore.example.com/v1/pets").mock(return_value=httpx.Response(201, json={"id": 1}))
        r = _call(test_client, "petstore", "create_pet", body={"name": "Rex"})
    assert r.status_code == 200, r.text
    assert json.loads(route.calls.last.request.content) == {"name": "Rex"}

    # approval: the write becomes a change request; it runs only after submit + final approval
    _mode(test_client, "petstore", "approval")
    r = _call(test_client, "petstore", "create_pet", body={"name": "Max"})
    assert r.status_code == 202 and r.json()["pending_approval"] is True
    change_id = r.json()["change"]["id"]
    h = {"X-CSRF-Token": "csrf-token"}
    assert test_client.post(f"/api/changes/{change_id}/submit", json={"confirmation": ""}, headers=h).status_code == 200
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://api.petstore.example.com/v1/pets").mock(return_value=httpx.Response(201, json={"id": 2}))
        r = test_client.post(f"/api/changes/{change_id}/approve", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done" and json.loads(route.calls.last.request.content) == {"name": "Max"}


def test_connection_scopes_and_roles(app_client, db_factory) -> None:  # type: ignore[no-untyped-def]
    app_client.cookies.set("msp_session", make_session(db_factory, ADMIN_ROLES, "admin@example.com"))
    _create(app_client, kind="openapi", name="Petstore", source={"text": _text("petstore.yaml")})
    good = app_client.post("/api/admin/api-keys", json={"name": "a", "scopes": ["petstore:read"]}).json()["key"]
    other = app_client.post("/api/admin/api-keys", json={"name": "b", "scopes": ["github:read"]}).json()["key"]
    app_client.cookies.clear()

    with respx.mock() as mock:
        mock.get("https://api.petstore.example.com/v1/pets/1").mock(return_value=httpx.Response(200, json={}))
        ok = app_client.post("/api/connections/petstore/operations/show_pet_by_id/call", json={"args": {"petId": 1}},
                             headers={"X-API-Key": good})
    assert ok.status_code == 200, ok.text
    denied = app_client.post("/api/connections/petstore/operations/show_pet_by_id/call", json={"args": {"petId": 1}},
                             headers={"X-API-Key": other})
    assert denied.status_code == 403
    write = app_client.post("/api/connections/petstore/operations/create_pet/call", json={"args": {"body": {"name": "x"}}},
                            headers={"X-API-Key": good})
    assert write.status_code == 403

    # Editors can call but not create / delete connections (Approver only)
    app_client.cookies.set("msp_session", make_session(db_factory, EDITOR, "ed@example.com"))
    assert app_client.post("/api/connections", json={"kind": "openapi", "source": {"text": "x"}}).status_code == 403
    assert app_client.delete("/api/connections/petstore").status_code == 403
    assert app_client.get("/api/connections").status_code == 200


def test_reimport_keeps_choices(test_client) -> None:  # type: ignore[no-untyped-def]
    _create(test_client, kind="openapi", name="Petstore", source={"text": _text("petstore.yaml")})
    test_client.patch("/api/connections/petstore/operations/show_pet_by_id", json={"pinned": True})
    test_client.patch("/api/connections/petstore/operations/delete_pet", json={"enabled": False})
    out = test_client.post("/api/connections/petstore/reimport", json={}).json()
    assert out["operations"] == 4 and out["added"] == 0 and out["removed"] == 0
    assert test_client.get("/api/connections/petstore/operations/show_pet_by_id").json()["pinned"] is True
    assert _call(test_client, "petstore", "delete_pet", petId=1).status_code == 403  # disabled stays disabled


def test_ssrf_guard(test_client) -> None:  # type: ignore[no-untyped-def]
    r = test_client.post("/api/connections", json={"kind": "openapi", "name": "x",
                                                   "source": {"url": "http://169.254.169.254/latest/spec.json"}})
    assert r.status_code == 400 and "link-local" in r.text
    _create(test_client, kind="openapi", name="Meta", base_url="http://169.254.169.254/latest",
            source={"text": _text("petstore.yaml")})
    r = _call(test_client, "meta", "show_pet_by_id", petId=1)
    assert r.status_code == 400 and "link-local" in r.text


def test_dry_run_saves_nothing(test_client) -> None:  # type: ignore[no-untyped-def]
    r = test_client.post("/api/connections?dry_run=true", json={"kind": "openapi", "name": "Petstore",
                                                                "source": {"text": _text("petstore.yaml")}})
    assert r.status_code == 200 and r.json()["dry_run"] is True and r.json()["operations"] == 4
    assert r.json()["auth_hint"]["auth_type"] == "api_key"
    assert test_client.get("/api/connections").json() == []


# ── Postman ───────────────────────────────────────────────────────────────


def test_postman_import_and_calls(test_client) -> None:  # type: ignore[no-untyped-def]
    out = _create(test_client, kind="postman", name="Helpdesk", source={"text": _text("collection.postman.json")})
    assert out["operations"] == 4 and out["base_url"] == "https://helpdesk.example.com/api"
    assert out["auth_type"] == "api_key"
    conn = test_client.get("/api/connections/helpdesk").json()
    assert "apiKey" not in json.dumps(conn["settings"])  # secret-looking variable kept encrypted, not in settings
    assert "variables" in conn["secrets_set"]
    names = {o["name"] for o in test_client.get("/api/connections/helpdesk/operations").json()["operations"]}
    assert names == {"list_open_tickets", "get_ticket", "create_ticket", "login"}

    with respx.mock(assert_all_called=True) as mock:
        get = mock.get("https://helpdesk.example.com/api/tickets/1001").mock(return_value=httpx.Response(200, json={"id": 1001}))
        lst = mock.get("https://helpdesk.example.com/api/tickets").mock(return_value=httpx.Response(200, json=[]))
        assert _call(test_client, "helpdesk", "get_ticket", id="1001").status_code == 200
        assert _call(test_client, "helpdesk", "list_open_tickets").status_code == 200
    assert get.calls.last.request.headers["X-API-Key"] == "collection-secret-123"
    assert dict(lst.calls.last.request.url.params) == {"status": "open", "size": "25"}

    _mode(test_client, "helpdesk", "direct")
    with respx.mock(assert_all_called=True) as mock:
        post = mock.post("https://helpdesk.example.com/api/tickets").mock(return_value=httpx.Response(201, json={"id": 5}))
        r = _call(test_client, "helpdesk", "create_ticket", summary='Printer "jammed"')
        login = mock.post("https://helpdesk.example.com/api/login").mock(return_value=httpx.Response(200, json={}))
        _call(test_client, "helpdesk", "login", body={"username": "demo", "password": "pw"})
    assert r.status_code == 200, r.text
    assert json.loads(post.calls.last.request.content) == {"summary": 'Printer "jammed"', "priority": 3}
    assert b"username=demo" in login.calls.last.request.content


# ── cURL ──────────────────────────────────────────────────────────────────


def test_curl_import_and_calls(test_client) -> None:  # type: ignore[no-untyped-def]
    preview = test_client.post("/api/connections?dry_run=true", json={
        "kind": "curl", "name": "RMM", "source": {"text": _text("commands.curl.txt")}}).json()
    assert preview["operations"] == 4 and preview["base_url"] == "https://rmm.example.com"
    assert preview["auth_hint"]["auth_type"] == "bearer" and preview["auth_hint"]["credentials_found"] == ["token"]

    _create(test_client, kind="curl", name="RMM", source={"text": _text("commands.curl.txt")})
    ops = {o["name"]: o for o in test_client.get("/api/connections/rmm/operations").json()["operations"]}
    assert set(ops) == {"list_devices", "get_device", "create_note", "odata_style_query"}

    with respx.mock(assert_all_called=True) as mock:
        dev = mock.get("https://rmm.example.com/api/v1/devices/D-9").mock(return_value=httpx.Response(200, json={}))
        odata = mock.get("https://rmm.example.com/odata/Devices").mock(return_value=httpx.Response(200, json={"value": []}))
        assert _call(test_client, "rmm", "get_device", device_id="D-9").status_code == 200
        assert _call(test_client, "rmm", "odata_style_query").status_code == 200
    assert dev.calls.last.request.headers["Authorization"] == "Bearer tok-abc-123"
    assert odata.calls.last.request.url.params["$filter"] == "Online eq true"  # $filter is not a placeholder

    _mode(test_client, "rmm", "direct")
    with respx.mock(assert_all_called=True) as mock:
        note = mock.post("https://rmm.example.com/api/v1/devices/D-9/notes").mock(return_value=httpx.Response(201, json={}))
        r = _call(test_client, "rmm", "create_note", device_id="D-9", NOTE_TEXT="Rebooted")
    assert r.status_code == 200, r.text
    assert json.loads(note.calls.last.request.content) == {"text": "Rebooted", "pinned": False}


# ── GraphQL ───────────────────────────────────────────────────────────────

SDL = """
type Query { ticket(id: ID!): Ticket  tickets(status: Status, first: Int = 10): [Ticket!]! }
type Mutation { createTicket(input: TicketInput!): Ticket }
type Ticket { id: ID! summary: String status: Status company: Company }
type Company { id: ID! name: String tickets(first: Int!): [Ticket] }
enum Status { OPEN CLOSED }
input TicketInput { summary: String! priority: Int }
"""


def test_graphql_sdl_import_and_call(test_client) -> None:  # type: ignore[no-untyped-def]
    _create(test_client, kind="graphql", name="Desk GQL", base_url="https://gql.example.com/graphql",
            source={"text": SDL}, auth={"type": "bearer", "token": "g-1"})
    ops = {o["name"]: o for o in test_client.get("/api/connections/desk_gql/operations").json()["operations"]}
    assert set(ops) == {"ticket", "tickets", "mutation_create_ticket"}
    assert ops["ticket"]["read_only"] and not ops["mutation_create_ticket"]["read_only"]
    detail = test_client.get("/api/connections/desk_gql/operations/tickets").json()
    assert detail["input_schema"]["properties"]["status"]["enum"] == ["OPEN", "CLOSED"]
    create = test_client.get("/api/connections/desk_gql/operations/mutation_create_ticket").json()
    assert create["input_schema"]["properties"]["input"]["required"] == ["summary"]

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://gql.example.com/graphql").mock(
            return_value=httpx.Response(200, json={"data": {"ticket": {"id": "1", "summary": "VPN down"}}}))
        r = _call(test_client, "desk_gql", "ticket", id="1")
    assert r.status_code == 200 and r.json()["result"]["data"] == {"id": "1", "summary": "VPN down"}
    sent = json.loads(route.calls.last.request.content)
    assert "ticket(id: $id)" in sent["query"] and "company { id name }" in sent["query"]
    assert "tickets" not in sent["query"].split("company", 1)[1]  # field with a required arg is skipped
    assert sent["variables"] == {"id": "1"} and route.calls.last.request.headers["Authorization"] == "Bearer g-1"

    with respx.mock() as mock:
        mock.post("https://gql.example.com/graphql").mock(
            return_value=httpx.Response(200, json={"errors": [{"message": "not found"}], "data": None}))
        assert _call(test_client, "desk_gql", "ticket", id="404").status_code == 502


def test_graphql_introspection_and_documents(test_client) -> None:  # type: ignore[no-untyped-def]
    from graphql import build_schema, introspection_from_schema

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://gql.example.com/graphql").mock(
            return_value=httpx.Response(200, json={"data": introspection_from_schema(build_schema(SDL))}))
        out = _create(test_client, kind="graphql", name="Introspected", base_url="https://gql.example.com/graphql")
    assert out["operations"] == 3

    docs = ("query OpenTickets($status: Status = OPEN) { tickets(status: $status) { id } }\n"
            "mutation CloseTicket($id: ID!) { closeTicket(id: $id) { id } }")
    out = _create(test_client, kind="graphql", name="Mine", base_url="https://gql.example.com/graphql",
                  source={"text": docs})
    ops = {o["name"]: o for o in test_client.get("/api/connections/mine/operations").json()["operations"]}
    assert set(ops) == {"open_tickets", "close_ticket"} and ops["open_tickets"]["read_only"]
    assert not ops["close_ticket"]["read_only"]


# ── OData ─────────────────────────────────────────────────────────────────


def test_odata_v2_sap(test_client) -> None:  # type: ignore[no-untyped-def]
    _create(test_client, kind="odata", name="SAP Test", base_url="https://sap.example.com/sap/opu/odata/sap/ZTEST_SRV/",
            source={"text": _text("odata_v2_sap.xml")}, auth={"type": "basic", "username": "u", "password": "p"})
    ops = {o["name"]: o for o in test_client.get("/api/connections/sap_test/operations").json()["operations"]}
    assert {"list_customer_set", "get_customer_set", "create_customer_set", "update_customer_set",
            "delete_customer_set", "list_invoice_set", "get_invoice_set", "get_top_customers"} == set(ops)
    assert "Customer Name" in test_client.get("/api/connections/sap_test/operations/list_customer_set").json()["description"] \
        or "Name1" in test_client.get("/api/connections/sap_test/operations/list_customer_set").json()["description"]
    get_schema = test_client.get("/api/connections/sap_test/operations/get_customer_set").json()["input_schema"]
    assert get_schema["properties"]["CustomerID"]["description"] == "Customer Number"  # SAP label

    base = "https://sap.example.com/sap/opu/odata/sap/ZTEST_SRV/"
    with respx.mock(assert_all_called=True) as mock:
        one = mock.get(f"{base}CustomerSet('C''1')").mock(
            return_value=httpx.Response(200, json={"d": {"CustomerID": "C'1", "Name1": "Acme"}}))
        inv = mock.get(f"{base}InvoiceSet(InvoiceID='9',Year=2024)").mock(return_value=httpx.Response(200, json={"d": {}}))
        lst = mock.get(f"{base}CustomerSet").mock(return_value=httpx.Response(200, json={"d": {"results": [{"CustomerID": "1"}]}}))
        r = _call(test_client, "sap_test", "get_customer_set", CustomerID="C'1")
        assert r.json()["result"]["data"] == {"CustomerID": "C'1", "Name1": "Acme"}
        _call(test_client, "sap_test", "get_invoice_set", InvoiceID="9", Year=2024)
        r = _call(test_client, "sap_test", "list_customer_set", filter="Name1 eq 'Acme'", top=5)
        assert r.json()["result"]["data"] == [{"CustomerID": "1"}]
    assert one.calls.last.request.url.params["$format"] == "json"
    assert lst.calls.last.request.url.params["$filter"] == "Name1 eq 'Acme'"
    assert inv.called

    _mode(test_client, "sap_test", "direct")
    with respx.mock(assert_all_called=True) as mock:
        mock.get(base).mock(return_value=httpx.Response(200, headers={"x-csrf-token": "tok-77"}))
        create = mock.post(f"{base}CustomerSet").mock(return_value=httpx.Response(201, json={"d": {"CustomerID": "2"}}))
        r = _call(test_client, "sap_test", "create_customer_set", body={"Name1": "New"})
    assert r.status_code == 200, r.text
    assert create.calls.last.request.headers["x-csrf-token"] == "tok-77"


def test_odata_v4_and_metadata_url(test_client) -> None:  # type: ignore[no-untyped-def]
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://odata.example.com/shop/$metadata").mock(
            return_value=httpx.Response(200, text=_text("odata_v4.xml")))
        out = _create(test_client, kind="odata", name="Shop", source={"url": "https://odata.example.com/shop"})
    assert out["base_url"] == "https://odata.example.com/shop/"
    ops = {o["name"]: o for o in test_client.get("/api/connections/shop/operations").json()["operations"]}
    assert ops["most_expensive"]["read_only"] and not ops["reset_prices"]["read_only"]
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://odata.example.com/shop/MostExpensive(Top=3)").mock(
            return_value=httpx.Response(200, json={"value": [{"ID": 1}]}))
        r = _call(test_client, "shop", "most_expensive", Top=3)
    assert r.json()["result"]["data"] == [{"ID": 1}]


# ── SOAP ──────────────────────────────────────────────────────────────────

SOAP_OK = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
<{op}Response xmlns="http://example.com/calc"><{op}Result>{value}</{op}Result></{op}Response>
</soap:Body></soap:Envelope>"""
SOAP_FAULT = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
<soap:Fault><faultcode>soap:Server</faultcode><faultstring>Overflow</faultstring></soap:Fault>
</soap:Body></soap:Envelope>"""


def test_soap_import_and_calls(test_client) -> None:  # type: ignore[no-untyped-def]
    out = _create(test_client, kind="soap", name="Calculator", source={"text": _text("calculator.wsdl")})
    assert out["base_url"] == "http://soap.example.com/calculator.asmx" and out["operations"] == 2
    ops = {o["name"]: o for o in test_client.get("/api/connections/calculator/operations").json()["operations"]}
    assert ops["get_version"]["read_only"] and not ops["add"]["read_only"]
    add = test_client.get("/api/connections/calculator/operations/add").json()
    assert add["input_schema"]["required"] == ["intA", "intB"]
    assert add["input_schema"]["properties"]["intA"]["type"] == "integer"
    assert add["description"] == "Adds two integers."

    url = "http://soap.example.com/calculator.asmx"
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(url).mock(return_value=httpx.Response(
            200, text=SOAP_OK.format(op="GetVersion", value="1.2.3"), headers={"content-type": "text/xml"}))
        r = _call(test_client, "calculator", "get_version")
    assert r.status_code == 200, r.text
    assert r.json()["result"]["data"] == "1.2.3"
    assert route.calls.last.request.headers["SOAPAction"].strip('"') == "http://example.com/calc/GetVersion"

    _mode(test_client, "calculator", "direct")
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(url).mock(return_value=httpx.Response(
            200, text=SOAP_OK.format(op="Add", value="5"), headers={"content-type": "text/xml"}))
        r = _call(test_client, "calculator", "add", intA=2, intB=3)
    assert r.json()["result"]["data"] == 5
    sent = route.calls.last.request.content.decode()
    assert "<ns0:intA>2</ns0:intA>" in sent and "<ns0:intB>3</ns0:intB>" in sent  # order + namespace by zeep

    with respx.mock() as mock:
        mock.post(url).mock(return_value=httpx.Response(500, text=SOAP_FAULT, headers={"content-type": "text/xml"}))
        r = _call(test_client, "calculator", "add", intA=1, intB=1)
    assert r.status_code == 502 and "Overflow" in r.text


# ── SQL ───────────────────────────────────────────────────────────────────


def _sqlite_db(path: Path) -> str:
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE tickets (id INTEGER PRIMARY KEY, summary TEXT, status TEXT, update_count INT);"
                      "INSERT INTO tickets VALUES (1,'VPN down','open',0),(2,'Printer','closed',2),(3,'Email','open',1);")
    con.commit()
    con.close()
    return "sqlite:///" + str(path).replace("\\", "/")


def test_sql_tools_are_read_only(test_client, tmp_path) -> None:  # type: ignore[no-untyped-def]
    dsn = _sqlite_db(tmp_path / "desk.db")
    saved = "- name: Open tickets\n  sql: SELECT id, summary FROM tickets WHERE status = :status ORDER BY id\n"
    out = _create(test_client, kind="sql", name="Desk DB", auth={"dsn": dsn}, source={"text": saved})
    assert out["operations"] == 5
    assert "sqlite" not in json.dumps(test_client.get("/api/connections/desk_db").json())  # DSN stays secret

    assert _call(test_client, "desk_db", "list_tables").json()["result"]["tables"] == ["tickets"]
    cols = _call(test_client, "desk_db", "describe_table", table="tickets").json()["result"]["columns"]
    assert [c["name"] for c in cols] == ["id", "summary", "status", "update_count"]
    assert len(_call(test_client, "desk_db", "sample_rows", table="tickets", limit=2).json()["result"]["rows"]) == 2
    r = _call(test_client, "desk_db", "run_query", sql="SELECT update_count FROM tickets WHERE id = :id",
              params={"id": 2})
    assert r.json()["result"]["rows"] == [{"update_count": 2}]
    assert _call(test_client, "desk_db", "open_tickets", status="open").json()["result"]["row_count"] == 2
    limited = _call(test_client, "desk_db", "run_query", sql="SELECT * FROM tickets", limit=1).json()["result"]
    assert limited["row_count"] == 1 and limited["truncated"] is True

    for bad, code in [("DELETE FROM tickets", 403), ("UPDATE tickets SET status='x'", 403),
                      ("SELECT 1; DROP TABLE tickets", 400),
                      ("WITH x AS (DELETE FROM tickets RETURNING *) SELECT * FROM x", 403),
                      ("SELECT * INTO t2 FROM tickets", 403), ("PRAGMA writable_schema = 1", 403),
                      ("select /* sneaky */ 1 -- \n; drop table tickets", 400)]:
        assert _call(test_client, "desk_db", "run_query", sql=bad).status_code == code, bad
    con = sqlite3.connect(tmp_path / "desk.db")
    assert con.execute("SELECT count(*) FROM tickets").fetchone()[0] == 3  # nothing changed
    con.close()


def test_sql_guard_unit() -> None:
    from app.connections.adapters.sql import check_read_only, normalize_dsn
    from app.connections.base import CallError

    check_read_only("SELECT 'delete from x' AS note, update_count FROM t")  # keywords inside strings / names ok
    check_read_only("WITH a AS (SELECT 1) SELECT * FROM a;")
    for bad in ("select pg_read_file('/etc/passwd')", "SELECT sleep(10)", "exec xp_cmdshell 'dir'",
                "SELECT * FROM t; DELETE FROM t"):
        with pytest.raises(CallError):
            check_read_only(bad)
    assert normalize_dsn("postgresql://u:p@db.example.com/x") == ("postgresql+asyncpg://u:p@db.example.com/x", True)
    assert normalize_dsn("mysql://u:p@db.example.com/x")[1] is False
    with pytest.raises(CallError):
        normalize_dsn("postgresql://u:p@169.254.169.254/x")


# ── MongoDB ───────────────────────────────────────────────────────────────


class _FakeColl:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs

    def find(self, filt, projection=None, sort=None, limit=0):  # type: ignore[no-untyped-def]
        return [d for d in self.docs if all(d.get(k) == v for k, v in (filt or {}).items())][: limit or None]

    def count_documents(self, filt):  # type: ignore[no-untyped-def]
        return len(self.find(filt))

    def aggregate(self, pipeline):  # type: ignore[no-untyped-def]
        return self.docs[: pipeline[-1]["$limit"]]


class _FakeDb(dict):
    def list_collection_names(self) -> list[str]:
        return list(self.keys())


def test_mongodb_tools(test_client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.connections.adapters import mongodb

    db = _FakeDb(devices=_FakeColl([{"_id": 1, "host": "PC-1", "online": True}, {"_id": 2, "host": "PC-2", "online": False}]))

    class _Client(dict):
        def close(self) -> None:
            pass

    monkeypatch.setattr(mongodb, "_client_factory", lambda dsn, timeout: _Client(inventory=db))
    _create(test_client, kind="mongodb", name="Inventory", auth={"dsn": "mongodb://u:p@mongo.example.com:27017/inventory"})
    assert _call(test_client, "inventory", "list_collections").json()["result"]["collections"] == ["devices"]
    r = _call(test_client, "inventory", "find", collection="devices", filter={"online": True})
    assert r.json()["result"]["documents"] == [{"_id": 1, "host": "PC-1", "online": True}]
    assert _call(test_client, "inventory", "count_documents", collection="devices").json()["result"]["count"] == 2
    for pipeline in ([{"$out": "x"}], [{"$match": {"$where": "true"}}], [{"$merge": {"into": "y"}}]):
        assert _call(test_client, "inventory", "aggregate", collection="devices", pipeline=pipeline).status_code == 403
    assert _call(test_client, "inventory", "find", collection="devices", filter={"$where": "1"}).status_code == 403


# ── MCP bridge ────────────────────────────────────────────────────────────


def _mcp_handler(request: httpx.Request) -> httpx.Response:
    msg = json.loads(request.content)
    method = msg.get("method")
    if method == "initialize":
        assert request.headers["Authorization"] == "Bearer remote-token"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "remote"}}},
            headers={"Mcp-Session-Id": "sess-1"})
    assert request.headers.get("Mcp-Session-Id") == "sess-1"
    if method == "notifications/initialized":
        return httpx.Response(202)
    if method == "tools/list":
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
            {"name": "get_weather", "description": "Weather by city", "annotations": {"readOnlyHint": True},
             "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
            {"name": "delete-thing", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}},
        ]}})
    if method == "tools/call":
        result = {"content": [{"type": "text", "text": f"Sunny in {msg['params']['arguments']['city']}"}]}
        body = "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    return httpx.Response(400)


def test_mcp_bridge(test_client) -> None:  # type: ignore[no-untyped-def]
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://mcp.example.com/mcp").mock(side_effect=_mcp_handler)
        out = _create(test_client, kind="mcp", name="Remote Tools", base_url="https://mcp.example.com/mcp",
                      auth={"type": "bearer", "token": "remote-token"})
        assert out["operations"] == 2 and out["read_operations"] == 1  # no readOnlyHint → treated as a write
        r = _call(test_client, "remote_tools", "get_weather", city="Paris")
        assert r.status_code == 200, r.text
        assert r.json()["result"]["content"][0]["text"] == "Sunny in Paris"
        assert _call(test_client, "remote_tools", "get_weather").status_code == 400  # inputSchema enforced
        assert _call(test_client, "remote_tools", "delete_thing", id="1").status_code == 403  # read-only default


# ── Audit ─────────────────────────────────────────────────────────────────


def test_calls_are_audited(test_client, db_factory) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from app.db.schema import AuditLog

    _create(test_client, kind="openapi", name="Petstore", source={"text": _text("petstore.yaml")})
    with respx.mock() as mock:
        mock.get("https://api.petstore.example.com/v1/pets/3").mock(return_value=httpx.Response(200, json={}))
        _call(test_client, "petstore", "show_pet_by_id", petId=3)
    _call(test_client, "petstore", "create_pet", body={"name": "x"})  # refused (read-only)

    async def rows() -> list[AuditLog]:
        async with db_factory() as db:
            return list((await db.execute(select(AuditLog).where(AuditLog.action.like("petstore.%")))).scalars())

    found = {(r.action, r.status) for r in asyncio.run(rows())}
    assert ("petstore.show_pet_by_id", 200) in found and ("petstore.create_pet", 403) in found
