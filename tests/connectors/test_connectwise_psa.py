"""Tests for ConnectWise PSA connector using respx HTTP mocking (no real network)."""

import json

import pytest
import respx
from httpx import Response

from app.config import Settings
from app.connectors import connectwise_psa
from app.connectors.connectwise_psa import ConnectWisePsaConnector

BASE = "https://na.myconnectwise.net/v4_6_release/apis/3.0"


@pytest.fixture
def cw_settings() -> Settings:
    return Settings(
        cw_company_id="mycompany",
        cw_public_key="pubkey",
        cw_private_key="privkey",
        cw_client_id="client-guid",
        cw_base_url=BASE,
    )


@pytest.fixture
def connector(cw_settings: Settings) -> ConnectWisePsaConnector:
    return ConnectWisePsaConnector(settings=cw_settings)


@pytest.fixture
def no_agreement_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh install: no managed-services / SentinelOne agreement types configured."""
    monkeypatch.setattr(connectwise_psa, "PSA_MANAGED_AGREEMENT_TYPE", "")
    monkeypatch.setattr(connectwise_psa, "PSA_S1_AGREEMENT_TYPE", "")


class TestConnectWisePsaConnector:
    """Tests for ConnectWisePsaConnector."""

    @respx.mock
    async def test_get_companies_basic_auth(self, connector: ConnectWisePsaConnector, no_agreement_rules: None) -> None:
        """Should send Basic auth and clientId headers; with no agreement rules every company is returned."""
        route = respx.get(f"{BASE}/company/companies").mock(
            return_value=Response(200, json=[{"id": 1, "name": "Acme Inc"}]))

        companies = await connector.get_companies()
        assert len(companies) == 1
        request = route.calls[0].request
        assert request.headers["Authorization"].startswith("Basic ")
        assert request.headers["clientId"] == "client-guid"

    @respx.mock
    async def test_get_companies_pagination(self, connector: ConnectWisePsaConnector, no_agreement_rules: None) -> None:
        """Should follow page-based pagination, stopping at a short page."""
        route = respx.get(f"{BASE}/company/companies")
        route.side_effect = [
            Response(200, json=[{"id": i, "name": f"Company {i}"} for i in range(1, 1001)]),
            Response(200, json=[{"id": 1001, "name": "Last Company"}]),
        ]
        companies = await connector.get_companies()
        assert len(companies) == 1001
        assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]

    @respx.mock
    async def test_get_companies_filtered_by_agreements(
        self, connector: ConnectWisePsaConnector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a managed agreement type configured, only companies holding one are returned."""
        monkeypatch.setattr(connectwise_psa, "PSA_MANAGED_AGREEMENT_TYPE", "Managed Services")
        monkeypatch.setattr(connectwise_psa, "PSA_S1_AGREEMENT_TYPE", "")
        respx.get(f"{BASE}/company/companies").mock(return_value=Response(200, json=[
            {"id": 1, "name": "Managed Co"}, {"id": 2, "name": "Prospect Co"}]))
        agreements = respx.get(f"{BASE}/finance/agreements").mock(
            return_value=Response(200, json=[{"id": 9, "company": {"id": 1, "name": "Managed Co"}}]))
        companies = await connector.get_companies()
        assert [c.platform_id for c in companies] == ["1"]
        assert 'type/name = "Managed Services"' in agreements.calls[0].request.url.params["conditions"]

    @respx.mock
    async def test_get_devices(self, connector: ConnectWisePsaConnector, no_agreement_rules: None) -> None:
        """Should return managed workstations/servers from the configurations endpoint."""
        respx.get(f"{BASE}/company/configurations").mock(return_value=Response(200, json=[
            {"id": 42, "name": "WORKSTATION-01", "type": {"name": "Managed Workstation"}, "activeFlag": True,
             "serialNumber": "SN_CW", "macAddress": "aa:bb:cc:dd:ee:ff", "company": {"id": 1, "name": "Acme"},
             "customFields": [{"caption": "Endpoint ID", "value": "uuid-1"}]},
            {"id": 43, "name": "PRINTER-01", "type": {"name": "Printer"}, "activeFlag": True,
             "company": {"id": 1, "name": "Acme"}},
            {"id": 44, "name": "OLD-PC", "type": {"name": "Managed Workstation"}, "activeFlag": False,
             "company": {"id": 1, "name": "Acme"}},
        ]))
        devices = await connector.get_devices()
        assert len(devices) == 1
        assert devices[0].hostname == "WORKSTATION-01"
        assert devices[0].serial_number == "SN_CW"
        assert devices[0].endpoint_id == "uuid-1"

    @respx.mock
    async def test_create_company_url(self, connector: ConnectWisePsaConnector) -> None:
        """The base URL already ends in /apis/3.0 — the path must not be repeated."""
        route = respx.post(f"{BASE}/company/companies").mock(
            return_value=Response(201, json={"id": 77, "name": "New Co", "identifier": "NEWCO"}))
        out = await connector.create_company_on_platform("New Co")
        assert out and out["id"] == "77"
        assert json.loads(route.calls[0].request.content)["name"] == "New Co"
