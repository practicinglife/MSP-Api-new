"""Connection templates, the public API directory, and exporting / importing connection settings."""

import asyncio
import json

import pytest

from app.services import connection_templates as ct
from tests.conftest import ADMIN_ROLES, make_session

HALO = {"tenant": "acme", "client_id": "cid", "client_secret": "SUPER-SECRET-VALUE"}


@pytest.fixture
def admin(app_client, db_factory):  # type: ignore[no-untyped-def]
    app_client.cookies.set("msp_session", make_session(db_factory, ADMIN_ROLES))
    return app_client


# ── The catalog ────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("t", ct.load(), ids=lambda t: t["id"])
def test_every_template_builds_a_valid_body(t):  # type: ignore[no-untyped-def]
    from app.connections.adapters import KINDS
    from app.connections.http import AUTH_TYPES

    assert t["kind"] in KINDS and t["category"] in ct.CATEGORIES
    values = {f["key"]: f.get("default") or ("https://x.example/a" if "url" in f["key"] else "x1")
              for f in t.get("fields") or []}
    body = ct.build_body(t["id"], values)
    assert body["kind"] == t["kind"] and body["name"] == t["name"]
    assert body["auth"]["type"] in AUTH_TYPES
    assert "{" not in body["base_url"]  # every placeholder filled
    src = body["source"]
    if "url" in src:
        assert src["url"].startswith("https://") and "{" not in src["url"]
    for method, path in ct.suggested(t["id"]):
        assert method in {"GET", "POST", "PUT", "PATCH", "DELETE"} and path.startswith("/")


def test_starter_definitions_parse_into_operations():
    from app.connections.adapters.openapi import build_operations, parse_document

    starters = [t for t in ct.load() if "file" in (t.get("source") or {})]
    assert len(starters) >= 10
    for t in starters:
        ops, _ = build_operations(parse_document((ct.TEMPLATES_DIR / t["source"]["file"]).read_text(encoding="utf-8")))
        assert ops, t["id"]
        targets = {(o.method.upper(), o.target) for o in ops}
        assert set(ct.suggested(t["id"])) <= targets, t["id"]  # suggested tools exist in the starter


def test_build_body_details():
    with pytest.raises(ct.TemplateError, match="Fill in"):
        ct.build_body("github", {})
    z = ct.build_body("zendesk", {"subdomain": "acme", "email": "a@b.c", "api_token": "T"})
    assert z["auth"] == {"type": "basic", "username": "a@b.c/token", "password": "T"}
    assert z["source"]["use_auth"] is False  # never send credentials to a definition on another host
    pg = ct.build_body("postgresql", {"host": "db", "database": "x", "username": "u", "password": "p@ss/w"})
    assert pg["auth"]["dsn"] == "postgresql://u:p%40ss%2Fw@db:5432/x"
    assert ct.build_body("mcp_server", {"url": "https://m.example/mcp"})["auth"] == {"type": "none"}
    assert "extra_headers" not in ct.build_body("xero", {"client_id": "i", "client_secret": "s"})["settings"]
    assert ct.build_body("notion", {"token": "t"})["settings"]["extra_headers"] == {"Notion-Version": "2022-06-28"}
    assert ct.build_body("any_openapi", {"definition_url": "https://x.example/o.json"},
                         auth_override={"type": "bearer", "token": "t"})["auth"]["type"] == "bearer"
    assert [t["id"] for t in ct.search("microsoft 365")] == ["microsoft_graph"]
    assert {t["id"] for t in ct.search("", "data")} >= {"postgresql", "any_openapi"}


# ── From a template, through the API ───────────────────────────────────────────────────────────────────────


def test_from_template_preview_save_pin_and_free_slug(admin):  # type: ignore[no-untyped-def]
    assert any(t["id"] == "halo_psa" for t in admin.get("/api/connections/templates").json()["templates"])
    r = admin.post("/api/connections/from-template?dry_run=true", json={"template": "halo_psa", "values": HALO})
    assert r.status_code == 200 and r.json()["dry_run"] and r.json()["operations"] == 7, r.text

    r = admin.post("/api/connections/from-template", json={"template": "halo_psa", "values": HALO})
    body = r.json()
    assert r.status_code == 200 and body["created"] and body["slug"] == "halo_psa", r.text
    assert len(body["pinned"]) == 3
    conn = admin.get("/api/connections/halo_psa").json()
    assert conn["auth_type"] == "oauth2_client_credentials" and conn["secrets_set"] == ["client_id", "client_secret"]
    assert conn["settings"]["token_url"] == "https://acme.halopsa.com/auth/token"

    again = admin.post("/api/connections/from-template", json={"template": "halo_psa", "values": HALO}).json()
    assert again["slug"] == "halo_psa_2"  # the same API twice gets its own name

    bad = admin.post("/api/connections/from-template", json={"template": "halo_psa", "values": {}})
    assert bad.status_code == 400 and "Fill in" in bad.json()["detail"]


def test_reserved_route_names_are_not_slugs(admin):  # type: ignore[no-untyped-def]
    from app.connections.service import RESERVED_SLUGS

    assert {"templates", "directory", "from_template", "import", "export"} <= RESERVED_SLUGS


# ── Public directory ────────────────────────────────────────────────────────────────────────────────────────

LIST = {
    "weather.example:forecast": {"preferred": "1.0", "versions": {"1.0": {
        "swaggerUrl": "https://api.apis.guru/v2/specs/weather.example/forecast/1.0/openapi.json",
        "info": {"title": "Forecast API", "description": "Weather forecasts", "x-apisguru-categories": ["weather"]}}}},
    "acme.example": {"preferred": "2", "versions": {"2": {
        "swaggerUrl": "https://api.apis.guru/v2/specs/acme.example/2/openapi.json", "info": {"title": "Acme"}}}},
}


def test_directory_search_and_cache(admin, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from app.services import api_directory

    monkeypatch.setenv("MSP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(api_directory, "_memory", None)
    calls = []

    async def fake_download():  # type: ignore[no-untyped-def]
        calls.append(1)
        return LIST

    monkeypatch.setattr(api_directory, "_download", fake_download)
    r = admin.get("/api/connections/directory?q=weather").json()
    assert [e["title"] for e in r["results"]] == ["Forecast API"]
    assert r["results"][0]["spec_url"].endswith("/openapi.json") and (tmp_path / "api-directory.json").exists()
    monkeypatch.setattr(api_directory, "_memory", None)
    admin.get("/api/connections/directory?q=acme")
    assert calls == [1]  # second search came from the cache file

    monkeypatch.setenv("MSP_API_DIRECTORY", "off")
    assert admin.get("/api/connections/directory?q=x").status_code == 503


def test_directory_offline_message(admin, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from app.services import api_directory

    monkeypatch.setenv("MSP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(api_directory, "_memory", None)

    async def offline():  # type: ignore[no-untyped-def]
        raise OSError("no route to host")

    monkeypatch.setattr(api_directory, "_download", offline)
    r = admin.get("/api/connections/directory?q=x")
    assert r.status_code == 503 and "online" in r.json()["detail"]


# ── Export / import ─────────────────────────────────────────────────────────────────────────────────────────


def test_export_import_round_trip_without_secrets(admin):  # type: ignore[no-untyped-def]
    admin.post("/api/connections/from-template", json={"template": "halo_psa", "values": HALO})
    assert admin.put("/api/admin/write-modes/halo_psa", json={"write_mode": "approval"}).status_code == 200

    exported = admin.get("/api/admin/connections/export").json()
    assert "SUPER-SECRET-VALUE" not in json.dumps(exported)
    item = next(c for c in exported["connections"] if c["slug"] == "halo_psa")
    assert item["write_mode"] == "approval" and len(item["pinned"]) == 3 and item["needs_credentials"]

    assert admin.delete("/api/connections/halo_psa").status_code == 200
    plan = admin.post("/api/admin/connections/import?dry_run=true", json=exported).json()
    assert [r["action"] for r in plan["results"] if r["slug"] == "halo_psa"] == ["create"]
    assert admin.get("/api/connections/halo_psa").status_code == 404  # dry run created nothing

    done = admin.post("/api/admin/connections/import", json=exported).json()
    row = next(r for r in done["results"] if r["slug"] == "halo_psa")
    assert row["action"] == "created" and row["pinned"] == 3 and "halo_psa" in done["needs_credentials"]
    conn = admin.get("/api/connections/halo_psa").json()
    assert conn["auth_type"] == "oauth2_client_credentials" and conn["secrets_set"] == []
    assert conn["settings"]["token_url"] == "https://acme.halopsa.com/auth/token"
    modes = admin.get("/api/admin/write-modes").json()["platforms"]
    assert modes["halo_psa"]["write_mode"] == "approval"

    again = admin.post("/api/admin/connections/import", json=exported).json()
    assert next(r for r in again["results"] if r["slug"] == "halo_psa")["action"] == "skipped"
    assert admin.post("/api/admin/connections/import", json={"format": "nope"}).status_code == 400


def test_export_and_import_need_an_approver(app_client, db_factory):  # type: ignore[no-untyped-def]
    app_client.cookies.set("msp_session", make_session(db_factory, ["Reader"]))
    assert app_client.get("/api/admin/connections/export").status_code == 403
    assert app_client.post("/api/connections/from-template", json={"template": "halo_psa", "values": HALO}).status_code == 403


# ── Small engine changes the templates need ─────────────────────────────────────────────────────────────────


def test_yaml_with_bare_equals_value_parses():
    from app.connections.adapters.openapi import parse_document

    assert parse_document("openapi: 3.0.0\npaths: {}\nx:\n  - change: =\n")["x"] == [{"change": "="}]


def test_extra_headers_are_sent_with_any_sign_in():
    from app.connections.http import ConnCtx, auth_headers_and_params

    ctx = ConnCtx(id="x", slug="n", kind="openapi", auth_type="bearer", secrets={"token": "t"},
                  settings={"extra_headers": {"Notion-Version": "2022-06-28", "Empty": ""}})
    headers, _ = asyncio.run(auth_headers_and_params(ctx))
    assert headers == {"Notion-Version": "2022-06-28", "Authorization": "Bearer t"}


# ── Your MSP tools as templates ─────────────────────────────────────────────────────────────────────────────


def test_msp_tools_are_found_by_their_other_names():
    found = {q: [t["id"] for t in ct.search(q)] for q in ("control", "platform", "eset", "s1", "psa")}
    assert found["control"] == ["screenconnect"] and found["platform"] == ["connectwise_asio"]
    assert found["eset"] == ["eset_msp", "eset_protect"] and found["s1"] == ["sentinelone"]
    assert found["psa"][0] == "connectwise_psa"
    assert all(t["builtin"] for t in ct.search("", "msp")[:6])


def test_msp_tool_bodies():
    psa = ct.build_body("connectwise_psa", {"company_id": "acme", "public_key": "pub", "private_key": "pk", "client_id": "c1"})
    assert psa["auth"] == {"type": "basic", "username": "acme+pub", "password": "pk"}
    assert psa["settings"]["extra_headers"] == {"clientId": "c1"} and psa["slug"] == "cw_psa"
    assert psa["base_url"] == "https://na.myconnectwise.net/v4_6_release/apis/3.0" and psa["source"]["text"]
    assert ct.build_body("connectwise_psa", {"company_id": "a", "public_key": "b", "private_key": "c", "client_id": "d"},
                         name="Second PSA").get("slug") is None  # renamed → slug from the new name
    asio = ct.build_body("connectwise_asio", {"client_id": "i", "client_secret": "s"})["auth"]
    assert asio["client_auth"] == "json" and asio["token_url"] == "https://openapi.service.itsupport247.net/v1/token"
    ema = ct.build_body("eset_msp", {"username": "u", "password": "p"})["auth"]
    assert ema == {"type": "token_login", "username": "u", "password": "p", "login_url": "https://mspapi.eset.com/api/Token/Get"}
    protect = ct.build_body("eset_protect", {"region": "eu", "username": "u", "password": "p"})
    assert protect["auth"]["type"] == "oauth2_password" and protect["base_url"] == "https://eu.device-management.eset.systems"
    sc = ct.build_body("screenconnect", {"instance_url": "https://acme.screenconnect.com", "auth_secret": "S"})
    assert sc["auth"] == {"type": "headers", "headers": {"CTRLAuthHeader": "S"}}
    assert sc["settings"]["extra_headers"] == {"Origin": "https://acme.screenconnect.com"}
    assert sc["base_url"].endswith("/App_Extensions/2d558935-686a-4bd0-9991-07539f5fe749/Service.ashx")


def test_screenconnect_post_reads_are_marked_read_only(admin):  # type: ignore[no-untyped-def]
    r = admin.post("/api/connections/from-template", json={"template": "screenconnect", "values": {
        "instance_url": "https://acme.screenconnect.com", "auth_secret": "S"}}).json()
    assert r["slug"] == "cw_control" and len(r["pinned"]) == 2, r
    ops = {o["name"]: o for o in admin.get("/api/connections/cw_control/operations?limit=50").json()["operations"]}
    by_target = {(o["method"], o["target"]): o for o in ops.values()}
    assert by_target[("POST", "/GetSessionsByFilter")]["read_only"]
    assert not by_target[("POST", "/SendCommandToSession")]["read_only"]


def test_template_post_reads_skip_the_write_mode_but_hand_labels_cannot(admin):  # type: ignore[no-untyped-def]
    import httpx
    import respx

    admin.post("/api/connections/from-template", json={"template": "screenconnect", "values": {
        "instance_url": "https://acme.screenconnect.com", "auth_secret": "S"}})
    assert admin.put("/api/admin/write-modes/cw_control", json={"write_mode": "approval"}).status_code == 200
    ops = admin.get("/api/connections/cw_control/operations?limit=50").json()["operations"]
    by_target = {(o["method"], o["target"]): o["name"] for o in ops}
    read, write = by_target[("POST", "/GetSessionsByFilter")], by_target[("POST", "/SendCommandToSession")]
    r = admin.patch(f"/api/connections/cw_control/operations/{write}", json={"read_only": True})
    assert r.status_code == 400 and "write mode" in r.json()["detail"]
    # switching a template read off and back on is fine; re-importing keeps it
    assert admin.patch(f"/api/connections/cw_control/operations/{read}", json={"read_only": False}).status_code == 200
    assert admin.patch(f"/api/connections/cw_control/operations/{read}", json={"read_only": True}).status_code == 200
    assert admin.post("/api/connections/cw_control/reimport", json={}).status_code == 200
    key = admin.post("/api/admin/api-keys", json={"name": "r", "scopes": ["cw_control:read"]}).json()["key"]
    admin.cookies.clear()

    base = "https://acme.screenconnect.com/App_Extensions/2d558935-686a-4bd0-9991-07539f5fe749/Service.ashx"
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{base}/GetSessionsByFilter").mock(return_value=httpx.Response(200, json=[]))
        r = admin.post(f"/api/connections/cw_control/operations/{read}/call",
                       json={"args": {"body": ["SessionType = 'Access'"]}}, headers={"X-API-Key": key})
        assert r.status_code == 200 and route.called, r.text


# ── The sign-in types the MSP tools need ────────────────────────────────────────────────────────────────────


def _ctx(auth_type, secrets, settings):  # type: ignore[no-untyped-def]
    from app.connections.http import ConnCtx

    return ConnCtx(id=f"t-{auth_type}", slug="x", kind="openapi", auth_type=auth_type, secrets=secrets,
                   settings=settings)


def test_oauth2_client_credentials_as_json():
    import respx

    from app.connections.http import _token_cache, auth_headers_and_params

    _token_cache.clear()
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://asio.example.test/v1/token").respond(200, json={"access_token": "A1", "expires_in": 3600})
        h, _ = asyncio.run(auth_headers_and_params(_ctx("oauth2_client_credentials", {"client_id": "i", "client_secret": "s"},
                                                        {"token_url": "https://asio.example.test/v1/token", "client_auth": "json", "scope": "x.read"})))
    assert h["Authorization"] == "Bearer A1"
    assert json.loads(route.calls[0].request.content) == {"grant_type": "client_credentials", "client_id": "i",
                                                          "client_secret": "s", "scope": "x.read"}


def test_oauth2_password_grant():
    import respx

    from app.connections.http import _token_cache, auth_headers_and_params

    _token_cache.clear()
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://iam.example.test/oauth/token").respond(200, json={"access_token": "P1"})
        h, _ = asyncio.run(auth_headers_and_params(_ctx("oauth2_password", {"username": "u@x", "password": "pw"},
                                                        {"token_url": "https://iam.example.test/oauth/token"})))
    assert h["Authorization"] == "Bearer P1"
    body = route.calls[0].request.content.decode()
    assert "grant_type=password" in body and "username=u%40x" in body and "Authorization" not in route.calls[0].request.headers


def test_token_login():
    import respx

    from app.connections.base import CallError
    from app.connections.http import _token_cache, auth_headers_and_params

    _token_cache.clear()
    ctx = _ctx("token_login", {"username": "api", "password": "pw"}, {"login_url": "https://ema.example.test/api/Token/Get"})
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://ema.example.test/api/Token/Get").respond(200, json={"accessToken": "E1"})
        h, _ = asyncio.run(auth_headers_and_params(ctx))
        asyncio.run(auth_headers_and_params(ctx))  # cached: one login
    assert h["Authorization"] == "Bearer E1" and len(route.calls) == 1
    assert json.loads(route.calls[0].request.content) == {"username": "api", "password": "pw"}

    _token_cache.clear()
    with respx.mock() as mock, pytest.raises(CallError, match="HTTP 401"):
        mock.post("https://ema.example.test/api/Token/Get").respond(401, json={})
        asyncio.run(auth_headers_and_params(ctx))


def test_read_only_marks_exist_in_the_definitions():
    from app.connections.adapters.openapi import build_operations, parse_document

    for t in ct.load():
        marks = set(ct.read_only(t["id"]))
        if not marks:
            continue
        ops, _ = build_operations(parse_document(ct.definition_file(t).read_text(encoding="utf-8-sig")))
        assert marks <= {(o.method.upper(), o.target) for o in ops}, t["id"]


def test_headers_the_connection_sends_are_not_required_arguments():
    from app.connections.service import _without_supplied_headers
    from app.db.schema import Connection, Operation

    op = Operation(input_schema={"type": "object", "properties": {"clientId": {}, "id": {}}, "required": ["clientId", "id"]},
                   binding={"params": [{"name": "clientId", "in": "header"}, {"name": "id", "in": "path"}]})
    psa = Connection(auth_type="basic", settings={"extra_headers": {"clientId": "c1"}}, secrets={})
    assert _without_supplied_headers(op, psa)["required"] == ["id"]
    other = Connection(auth_type="basic", settings={}, secrets={})
    assert _without_supplied_headers(op, other)["required"] == ["clientId", "id"]
