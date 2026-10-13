"""Tests for ScreenConnect connector using respx HTTP mocking (no real network)."""

import json

import httpx
import pytest
import respx
from httpx import Response

from app.config import Settings
from app.connectors.screenconnect import ScreenConnectConnector

BASE = "https://test.screenconnect.com"
EXT = "2d558935-686a-4bd0-9991-07539f5fe749"  # the RESTful API Manager extension's default id
SESSIONS_URL = f"{BASE}/App_Extensions/{EXT}/Service.ashx/GetSessionsByFilter"


@pytest.fixture
def sc_settings() -> Settings:
    return Settings(screenconnect_base_url=BASE, screenconnect_auth_token="ctrl_tok_abc")


@pytest.fixture
def connector(sc_settings: Settings) -> ScreenConnectConnector:
    return ScreenConnectConnector(settings=sc_settings)


class TestScreenConnectConnector:
    """Tests for ScreenConnectConnector."""

    @respx.mock
    async def test_get_devices_auth_header(self, connector: ScreenConnectConnector) -> None:
        """Should POST the session filter with the CTRLAuthHeader secret and the instance Origin."""
        route = respx.post(SESSIONS_URL).mock(return_value=Response(200, json=[]))
        await connector.get_devices()
        request = route.calls[0].request
        assert request.headers["CTRLAuthHeader"] == "ctrl_tok_abc"
        assert request.headers["Origin"] == BASE
        assert json.loads(request.content) == [" "]  # wildcard filter (an empty string is rejected)

    @respx.mock
    async def test_get_devices_returns_records(self, connector: ScreenConnectConnector) -> None:
        """Should parse sessions into DeviceRecords: company / site / Asio site id from custom properties."""
        respx.post(SESSIONS_URL).mock(return_value=Response(200, json=[
            {"SessionID": "sess-001", "GuestMachineName": "WORKSTATION.corp.local",
             "GuestMachineSerialNumber": "SN_SC", "GuestHardwareNetworkAddress": ["aa:bb:cc:00:11:22"],
             "CustomPropertyValues": ["Acme Inc", "Main Office", "", "", "4021"]},
            {"SessionID": "sess-002", "GuestMachineName": "", "Name": "LAPTOP-7",
             "GuestHardwareNetworkAddress": "aa-bb-cc-00-11-33; aa-bb-cc-00-11-44"},
        ]))
        devices = await connector.get_devices()
        assert len(devices) == 2
        first, second = devices
        assert first.platform_id == "sess-001" and first.hostname == "WORKSTATION"
        assert "AABBCC001122" in first.mac_addresses
        assert first.site_name == "Main Office" and first.site_id == "4021"
        assert second.hostname == "LAPTOP-7"  # falls back to the session name
        assert second.mac_addresses == ["AABBCC001133", "AABBCC001144"]

    @respx.mock
    async def test_get_companies_deduplicated(self, connector: ScreenConnectConnector) -> None:
        """Should deduplicate companies derived from sessions."""
        respx.post(SESSIONS_URL).mock(return_value=Response(200, json=[
            {"CustomPropertyValues": ["Acme Inc"]},
            {"CustomPropertyValues": ["Acme Inc"]},  # duplicate
            {"CustomPropertyValues": ["Beta LLC"]},
            {"CustomPropertyValues": []},           # no company
        ]))
        companies = await connector.get_companies()
        assert len({c.name for c in companies}) == 2 == len(companies)

    @respx.mock
    async def test_rejected_secret_is_an_error(self, connector: ScreenConnectConnector) -> None:
        respx.post(SESSIONS_URL).mock(return_value=Response(403, json={"error": "denied"}))
        with pytest.raises(httpx.HTTPStatusError):
            await connector.get_devices()
