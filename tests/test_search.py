"""Phase 3 — ranked operation search and the Connections page. Local only (fixtures, no network)."""

from __future__ import annotations

from pathlib import Path

from app.connections.search import Index, stem, tokenize
from tests.conftest import ADMIN_ROLES, make_session

FIX = Path(__file__).parent / "fixtures" / "connections"

OPS = [
    {"name": "list_tickets", "title": "List service tickets", "target": "/service/tickets", "tags": ["Service"],
     "description": "Returns open and closed tickets", "params": ["conditions", "page"], "read_only": True},
    {"name": "create_ticket", "title": "Create a ticket", "target": "/service/tickets", "tags": ["Service"],
     "description": "Opens a new service ticket", "params": ["summary", "board"], "read_only": False},
    {"name": "delete_configuration", "title": "Delete a configuration", "target": "/company/configurations/{id}",
     "tags": ["Company"], "description": "Removes a device record", "params": ["id"], "read_only": False},
    {"name": "get_company", "title": "Get company", "target": "/company/companies/{id}", "tags": ["Company"],
     "description": "One customer company", "params": ["id"], "read_only": True},
    {"name": "list_invoices", "title": "List invoices", "target": "/finance/invoices", "tags": ["Finance"],
     "description": "Billing documents", "params": ["companyId"], "read_only": True, "deprecated": True},
    {"name": "list_invoices_v2", "title": "List invoices", "target": "/finance/v2/invoices", "tags": ["Finance"],
     "description": "Billing documents", "params": ["companyId"], "read_only": True, "pinned": True},
]


def _names(hits):  # type: ignore[no-untyped-def]
    return [rec["name"] for _, rec, _ in hits]


def test_tokenize_and_stem() -> None:
    assert tokenize("getCompanyById") == ["get", "company", "id"]  # "by" is a stop word
    assert tokenize("/service/tickets/{ticketId}") == ["service", "ticket", "ticket", "id"]
    assert stem("companies") == "company" and stem("devices") == "device" and stem("notes") == "note"
    assert stem("boxes") == "box" and stem("address") == "address"


def test_ranking_basics() -> None:
    idx = Index(OPS)
    assert _names(idx.search("list tickets"))[0] == "list_tickets"
    assert _names(idx.search("create ticket"))[0] == "create_ticket"          # write verb → write op first
    assert _names(idx.search("show tickets"))[0] == "list_tickets"            # synonym show → list, read intent
    assert _names(idx.search("remove device"))[0] == "delete_configuration"   # remove→delete, device→configuration
    assert _names(idx.search("customer"))[0] == "get_company"                 # customer → company
    assert _names(idx.search("tikcets"))[0] in ("list_tickets", "create_ticket")  # typo
    assert "list_tickets" in _names(idx.search("tick"))                       # prefix
    assert _names(idx.search("invoices"))[0] == "list_invoices_v2"            # pinned beats deprecated twin
    assert idx.search("zzzz qqqq") == []


def test_all_words_beat_one_word() -> None:
    idx = Index(OPS)
    hits = idx.search("company configuration delete")
    assert _names(hits)[0] == "delete_configuration"


def test_predicate_filters() -> None:
    idx = Index(OPS)
    hits = idx.search("ticket", predicate=lambda r: not r["read_only"])
    assert _names(hits) == ["create_ticket"]


def _setup(client) -> None:  # type: ignore[no-untyped-def]
    for kind, name, file in (("openapi", "Petstore", "petstore.yaml"), ("postman", "Helpdesk", "collection.postman.json"),
                             ("soap", "Calculator", "calculator.wsdl")):
        r = client.post("/api/connections", json={"kind": kind, "name": name,
                                                  "source": {"text": (FIX / file).read_text(encoding="utf-8")}})
        assert r.status_code == 200, r.text


def test_global_search_api(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    r = test_client.get("/api/search/operations", params={"q": "remove pet"}).json()
    assert r["results"][0]["name"] == "delete_pet" and r["results"][0]["connection"] == "petstore"
    r = test_client.get("/api/search/operations", params={"q": "open tickets"}).json()
    assert r["results"][0]["name"] == "list_open_tickets" and r["results"][0]["connection"] == "helpdesk"
    r = test_client.get("/api/search/operations", params={"q": "add numbers"}).json()  # SOAP Add (doc: "Adds two integers")
    assert r["results"][0]["name"] == "add"
    only = test_client.get("/api/search/operations", params={"q": "pet", "connection": "helpdesk"}).json()
    assert all(h["connection"] == "helpdesk" for h in only["results"])
    writes = test_client.get("/api/search/operations", params={"q": "pet", "read_only": "false"}).json()
    assert writes["results"] and all(not h["read_only"] for h in writes["results"])
    scoped = test_client.get("/api/connections/petstore/operations", params={"q": "info for pet"}).json()
    assert scoped["operations"][0]["name"] == "show_pet_by_id"


def test_search_index_follows_changes(test_client) -> None:  # type: ignore[no-untyped-def]
    _setup(test_client)
    assert test_client.get("/api/search/operations", params={"q": "pet", "pinned": "true"}).json()["count"] == 0
    test_client.patch("/api/connections/petstore/operations/show_pet_by_id", json={"pinned": True})
    pinned = test_client.get("/api/search/operations", params={"q": "pet", "pinned": "true"}).json()
    assert [h["name"] for h in pinned["results"]] == ["show_pet_by_id"]
    test_client.patch("/api/connections/petstore/operations/delete_pet", json={"enabled": False})
    names = [h["name"] for h in test_client.get("/api/search/operations", params={"q": "delete pet"}).json()["results"]]
    assert "delete_pet" not in names                     # disabled operations are hidden from search
    test_client.delete("/api/connections/calculator")
    assert test_client.get("/api/search/operations", params={"q": "add"}).json()["count"] == 0 or \
        all(h["connection"] != "calculator" for h in
            test_client.get("/api/search/operations", params={"q": "add"}).json()["results"])


def test_search_respects_scopes(app_client, db_factory) -> None:  # type: ignore[no-untyped-def]
    app_client.cookies.set("msp_session", make_session(db_factory, ADMIN_ROLES))
    _setup(app_client)
    key = app_client.post("/api/admin/api-keys", json={"name": "k", "scopes": ["helpdesk:read"]}).json()["key"]
    app_client.cookies.clear()
    res = app_client.get("/api/search/operations", params={"q": "pet ticket"}, headers={"X-API-Key": key}).json()
    assert res["results"] and {h["connection"] for h in res["results"]} == {"helpdesk"}


def test_discovery_search_is_ranked(test_client, db_factory) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    from app.db.repository import ApiDiscoveryRepository

    async def seed() -> None:
        async with db_factory() as db:
            await ApiDiscoveryRepository(db).replace_endpoints("demo", [
                {"path": "/inventory/widgets", "method": "GET", "summary": "List widgets", "description": "",
                 "tags": ["Inventory"], "parameters": []},
                {"path": "/service/tickets", "method": "GET", "summary": "List tickets", "description": "",
                 "tags": ["Service"], "parameters": []},
            ])

    asyncio.run(seed())
    hits = test_client.get("/api/discovery/search", params={"q": "inventry widget"}).json()  # typo + singular
    assert hits[0]["path"] == "/inventory/widgets"  # the old LIKE search found nothing for this


def test_connections_page(app_client, test_client) -> None:  # type: ignore[no-untyped-def]
    page = test_client.get("/connections")
    assert page.status_code == 200 and "Search all operations" in page.text
    test_client.cookies.clear()
    r = test_client.get("/connections", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/auth/login")
