"""Tests for SentinelOne connector using respx HTTP mocking."""

import httpx
import pytest
import respx
from httpx import Response

from app.config import Settings
from app.connectors.sentinelone import SentinelOneConnector


@pytest.fixture
def s1_settings() -> Settings:
    """Return test settings for SentinelOne."""
    return Settings(
        s1_base_url="https://test.sentinelone.net",
        s1_api_token="test-api-token",
    )


@pytest.fixture
def connector(s1_settings: Settings) -> SentinelOneConnector:
    """Return a connector using test settings."""
    return SentinelOneConnector(settings=s1_settings)


class TestSentinelOneGetDevices:
    """Tests for SentinelOneConnector.get_devices()."""

    @respx.mock
    async def test_successful_agent_list(self, connector: SentinelOneConnector) -> None:
        """Should return a list of DeviceRecords on successful response."""
        respx.get("https://test.sentinelone.net/web/api/v2.1/agents").mock(
            return_value=Response(
                200,
                json={
                    "data": [
                        {
                            "id": "agent1",
                            "computerName": "DESKTOP-001",
                            "serialNumber": "SN001",
                            "networkInterfaces": [{"physical": "aa:bb:cc:dd:ee:ff"}],
                            "siteId": "site1",
                        }
                    ],
                    "pagination": {"nextCursor": None},
                },
            )
        )
        devices = await connector.get_devices()
        assert len(devices) == 1
        assert devices[0].platform == "sentinelone"
        assert devices[0].platform_id == "agent1"
        assert devices[0].hostname == "DESKTOP-001"
        assert devices[0].serial_number == "SN001"
        assert "AABBCCDDEEFF" in devices[0].mac_addresses

    @respx.mock
    async def test_cursor_pagination(self, connector: SentinelOneConnector) -> None:
        """Should follow cursor pagination to retrieve all agents."""
        route = respx.get("https://test.sentinelone.net/web/api/v2.1/agents")
        route.side_effect = [
            Response(
                200,
                json={
                    "data": [{"id": "agent1", "computerName": "HOST1", "networkInterfaces": []}],
                    "pagination": {"nextCursor": "cursor_abc"},
                },
            ),
            Response(
                200,
                json={
                    "data": [{"id": "agent2", "computerName": "HOST2", "networkInterfaces": []}],
                    "pagination": {"nextCursor": None},
                },
            ),
        ]
        devices = await connector.get_devices()
        assert len(devices) == 2

    @respx.mock
    async def test_http_401_raises(self, connector: SentinelOneConnector) -> None:
        """Should raise an exception on HTTP 401."""
        respx.get("https://test.sentinelone.net/web/api/v2.1/agents").mock(
            return_value=Response(401, json={"error": "Unauthorized"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await connector.get_devices()

    @respx.mock
    async def test_correct_auth_header(self, connector: SentinelOneConnector) -> None:
        """Should send Authorization: ApiToken <token> header."""
        route = respx.get("https://test.sentinelone.net/web/api/v2.1/agents").mock(
            return_value=Response(
                200, json={"data": [], "pagination": {"nextCursor": None}}
            )
        )
        await connector.get_devices()
        assert route.called
        request = route.calls[0].request
        assert request.headers["Authorization"] == "ApiToken test-api-token"


class TestSentinelOneGetCompanies:
    """Tests for SentinelOneConnector.get_companies()."""

    @respx.mock
    async def test_successful_site_list(self, connector: SentinelOneConnector) -> None:
        """Should return a list of CompanyRecords from sites."""
        respx.get("https://test.sentinelone.net/web/api/v2.1/sites").mock(
            return_value=Response(
                200,
                json={
                    "data": {"sites": [{"id": "site1", "name": "Acme Corp"}]},
                    "pagination": {"nextCursor": None},
                },
            )
        )
        companies = await connector.get_companies()
        assert len(companies) == 1
        assert companies[0].platform_id == "site1"
