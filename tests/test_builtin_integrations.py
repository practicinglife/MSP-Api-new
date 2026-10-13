"""Built-in integrations are connections: one credential store, listed on /connections, live connectors follow."""

import asyncio

import pytest

from tests.conftest import ADMIN_ROLES, make_session

S1 = {"base_url": "https://s1.example.test", "api_token": "TOKEN-123"}


@pytest.fixture
def admin(app_client, db_factory):  # type: ignore[no-untyped-def]
    app_client.cookies.set("msp_session", make_session(db_factory, ADMIN_ROLES))
    return app_client


def _live() -> set[str]:
    from app.api.connector_health import get_connectors

    return {c.platform_name for c in get_connectors()}


def test_save_lists_as_connection_and_goes_live(admin):  # type: ignore[no-untyped-def]
    r = admin.post("/api/connectors/register", json={"platform": "sentinelone", "credentials": S1,
                                                     "test_connection": False})
    assert r.status_code == 200 and r.json()["saved"], r.text
    assert "sentinelone" in _live()

    conns = {c["slug"]: c for c in admin.get("/api/connections").json()}
    assert conns["sentinelone"]["kind"] == "builtin" and conns["sentinelone"]["secrets_set"] == ["api_token", "base_url"]

    cat = {c["platform"]: c for c in admin.get("/api/connectors/catalog").json()}
    s1 = cat["sentinelone"]
    assert s1["connected"] and s1["credentials_from"] == "app" and s1["role"] == "security"
    assert s1["values"]["api_token"] != "TOKEN-123"  # redacted
    assert cat["connectwise_psa"]["role"] == "management" and not cat["connectwise_psa"]["connected"]


def test_blank_secret_keeps_saved_value(admin, db_factory):  # type: ignore[no-untyped-def]
    admin.post("/api/connectors/register", json={"platform": "sentinelone", "credentials": S1, "test_connection": False})
    r = admin.post("/api/connectors/register", json={
        "platform": "sentinelone", "credentials": {"base_url": "https://s1b.example.test", "api_token": ""},
        "test_connection": False})
    assert r.status_code == 200, r.text
    from app.api.connector_health import get_connectors

    s1 = next(c for c in get_connectors() if c.platform_name == "sentinelone")
    assert s1._settings.s1_api_token == "TOKEN-123" and s1._settings.s1_base_url == "https://s1b.example.test"


def test_missing_required_fields_refused(admin):  # type: ignore[no-untyped-def]
    r = admin.post("/api/connectors/register", json={"platform": "sentinelone", "credentials": {"base_url": "x"},
                                                     "test_connection": False})
    assert r.status_code == 400 and "missing required fields" in r.json()["detail"].lower()


def test_generic_edit_cannot_change_builtin_credentials(admin):  # type: ignore[no-untyped-def]
    admin.post("/api/connectors/register", json={"platform": "sentinelone", "credentials": S1, "test_connection": False})
    r = admin.patch("/api/connections/sentinelone", json={"auth": {"type": "bearer", "token": "x"}})
    assert r.status_code == 400


def test_disable_and_forget(admin):  # type: ignore[no-untyped-def]
    admin.post("/api/connectors/register", json={"platform": "sentinelone", "credentials": S1, "test_connection": False})
    assert admin.patch("/api/connections/sentinelone", json={"enabled": False}).status_code == 200
    assert "sentinelone" not in _live()
    assert admin.patch("/api/connections/sentinelone", json={"enabled": True}).status_code == 200
    assert "sentinelone" in _live()

    r = admin.delete("/api/connectors/configs/sentinelone")
    assert r.status_code == 200 and not r.json()["still_connected_from_env"]
    assert "sentinelone" not in _live()
    assert "sentinelone" not in {c["slug"] for c in admin.get("/api/connections").json()}


def test_old_connector_configs_are_adopted(db_factory):  # type: ignore[no-untyped-def]
    from app.db.repository import ConnectorConfigRepository
    from app.services import builtin_integrations as b

    async def run():  # type: ignore[no-untyped-def]
        async with db_factory() as db:
            await ConnectorConfigRepository(db).upsert("sentinelone", S1)
        created = await b.adopt_existing()
        again = await b.adopt_existing()
        return created, again, [c.platform_name for c in await b.load_live_connectors()]

    created, again, live = asyncio.run(run())
    assert created == ["sentinelone"] and again == [] and "sentinelone" in live


def test_operations_come_from_the_vendor_definition_and_call_through_the_connector(monkeypatch):  # type: ignore[no-untyped-def]
    from app.connections.adapters import builtin
    from app.connections.http import ConnCtx

    ctx = ConnCtx(id="x", slug="screenconnect", kind="builtin", base_url="", auth_type="none", secrets={},
                  settings={"platform": "screenconnect"}, source={})
    result = asyncio.run(builtin.discover(ctx, {}))
    assert result.operations, result.warnings

    seen = {}

    async def fake_vendor_request(platform, method, path, params, body):  # type: ignore[no-untyped-def]
        seen.update(platform=platform, method=method, path=path, params=params, body=body)
        return {"response": {"ok": True}}

    monkeypatch.setattr("app.api.proxy.vendor_request", fake_vendor_request)
    binding = {"method": "GET", "path": "/agents/{id}", "params": [{"name": "id", "in": "path"},
                                                                  {"name": "limit", "in": "query"}]}
    out = asyncio.run(builtin.execute(ctx, binding, {"id": "a b", "limit": 5}))
    assert out == {"ok": True}
    assert seen == {"platform": "screenconnect", "method": "GET", "path": "/agents/a%20b", "params": {"limit": 5},
                    "body": None}


def test_getting_started_steps(admin, db_factory, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MSP_DATA_DIR", str(tmp_path))
    assert admin.get("/api/admin/getting-started").json()["step"] == "secure"  # before connecting anything
    assert admin.post("/api/admin/secure/step", json={"mode": "local"}).status_code == 200
    assert admin.get("/api/admin/getting-started").json()["step"] == "tools"
    admin.post("/api/connectors/register", json={"platform": "sentinelone", "credentials": S1, "test_connection": False})
    assert admin.get("/api/admin/getting-started").json()["step"] == "sync"
    from tests.test_company_linker import _seed_devices

    _seed_devices(db_factory, [("connectwise_psa", "Blue River Credit Fund", "BRCF-1"),
                               ("asio", "Blue River Credit Fund", "BRCF-1"),
                               ("sentinelone", "Zephyr", "ZP-1")])
    gs = admin.get("/api/admin/getting-started").json()
    assert gs["step"] == "match" and gs["pending"] == 1

    by_tool = admin.get("/api/companies/matching/by-tool").json()
    s1 = next(t for t in by_tool["tools"] if t["tool"] == "sentinelone")
    assert [(r["name"], r["status"]) for r in s1["rows"]] == [("zephyr", "needs_match")]
    assert by_tool["clients"] == ["blue river credit fund"]

    r = admin.post("/api/companies/matching/apply", json={"links": [{"name": "Zephyr", "target": "Blue River Credit Fund"}]})
    assert r.status_code == 200 and r.json()["saved"] == 1
    assert admin.get("/api/admin/getting-started").json()["step"] == "done"


def test_local_run_prepares_a_fresh_checkout(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from app import local_run

    monkeypatch.chdir(tmp_path)
    for var in ("MSP_MASTER_KEY", "DATABASE_URL", "MSP_PACKS", "MSP_EXTERNAL_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    notes = local_run.prepare(tmp_path / "data", port=8443)
    assert (tmp_path / "data" / "master.key").exists() and (tmp_path / "certs" / "server.crt").exists()
    import os

    assert os.environ["DATABASE_URL"].startswith("sqlite+aiosqlite:///") and "MSP_PACKS" not in os.environ
    assert os.environ["MSP_DATA_DIR"] == str(tmp_path / "data") and "not chosen yet" in notes["packs"]
    assert notes["url"] == "https://localhost:8443"
    key = os.environ["MSP_MASTER_KEY"]
    monkeypatch.delenv("MSP_MASTER_KEY")
    local_run.prepare(tmp_path / "data", port=8443)
    assert os.environ["MSP_MASTER_KEY"] == key  # the same key on every start


def test_eset_connect_is_a_detail_source_not_a_live_connector(admin, monkeypatch):  # type: ignore[no-untyped-def]
    from app.connectors.eset_connect import EsetConnectConnector

    async def fake_auth(self):  # type: ignore[no-untyped-def]
        return True

    async def fake_groups(self):  # type: ignore[no-untyped-def]
        return [{"uuid": "g1"}, {"uuid": "g2"}]

    monkeypatch.setattr(EsetConnectConnector, "authenticate", fake_auth)
    monkeypatch.setattr(EsetConnectConnector, "get_device_groups", fake_groups)
    creds = {"username": "u@example.test", "password": "pw", "base_url": "https://eu.device-management.eset.systems",
             "auth_url": "https://eu.business-account.iam.eset.systems"}
    r = admin.post("/api/connectors/register", json={"platform": "eset_connect", "credentials": creds})
    body = r.json()
    assert r.status_code == 200 and body["saved"], body
    assert body["test_result"]["companies_found"] == 2 and body["test_result"]["found_label"] == "device groups"
    assert "eset_connect" not in _live()  # reconciliation never asks it for companies
    cat = {c["platform"]: c for c in admin.get("/api/connectors/catalog").json()}
    assert cat["eset_connect"]["connected"] and cat["eset_connect"]["detail_source"]

    from app.services.builtin_integrations import detail_connector

    c = asyncio.run(detail_connector("eset_connect"))
    assert c._settings.eset_connect_base_url == "https://eu.device-management.eset.systems"


def test_catalog_test_uses_stored_credentials(admin, monkeypatch):  # type: ignore[no-untyped-def]
    admin.post("/api/connectors/register", json={"platform": "sentinelone", "credentials": S1, "test_connection": False})

    async def fake_test(connector):  # type: ignore[no-untyped-def]
        return {"platform": connector.platform_name, "status": "ok", "companies_found": 7, "latency_ms": 1,
                "token": connector._settings.s1_api_token}

    monkeypatch.setattr("app.api.connector_health._test_connector", fake_test)
    r = admin.post("/api/connectors/catalog/sentinelone/test")
    assert r.status_code == 200 and r.json()["companies_found"] == 7 and r.json()["token"] == "TOKEN-123"
    assert admin.post("/api/connectors/catalog/nope/test").status_code == 404
