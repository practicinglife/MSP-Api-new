"""Tests for Avast connector using respx HTTP mocking."""

import pytest
import respx
from httpx import Response

from app.config import Settings
from app.connectors.avast import _TOKEN_URL, AvastConnector


@pytest.fixture
def avast_settings() -> Settings:
    """Return test settings for Avast."""
    return Settings(
        avast_client_id="test-client-id",
        avast_client_secret="test-client-secret",
    )


@pytest.fixture
def connector(avast_settings: Settings) -> AvastConnector:
    """Return connector using test settings."""
    return AvastConnector(settings=avast_settings)


class TestAvastConnector:
    """Tests for AvastConnector."""

    @respx.mock
    async def test_oauth_token_exchange(self, connector: AvastConnector) -> None:
        """Should POST to token URL and store access token."""
        respx.post(_TOKEN_URL).mock(
            return_value=Response(200, json={"access_token": "tok123", "token_type": "Bearer"})
        )
        await connector.authenticate()
        assert connector._access_token == "tok123"

    @respx.mock
    async def test_get_companies(self, connector: AvastConnector) -> None:
        """Should return CompanyRecords after authenticating."""
        connector._access_token = "tok123"
        respx.get("https://businesshub.avast.com/companies").mock(
            return_value=Response(
                200,
                json=[{"id": "c1", "name": "Acme Inc"}],
            )
        )
        companies = await connector.get_companies()
        assert len(companies) == 1
        assert companies[0].platform == "avast"
        assert companies[0].platform_id == "c1"

    @respx.mock
    async def test_get_devices(self, connector: AvastConnector) -> None:
        """Should return DeviceRecords after authenticating."""
        connector._access_token = "tok123"
        respx.get("https://businesshub.avast.com/devices").mock(
            return_value=Response(
                200,
                json=[
                    {
                        "id": "d1",
                        "hostname": "WORKSTATION.local",
                        "serial_number": "SN001",
                        "mac_addresses": ["aa:bb:cc:dd:ee:ff"],
                        "company_id": "c1",
                    }
                ],
            )
        )
        devices = await connector.get_devices()
        assert len(devices) == 1
        assert devices[0].platform_id == "d1"
        assert devices[0].hostname == "WORKSTATION"
        assert "AABBCCDDEEFF" in devices[0].mac_addresses

    @respx.mock
    async def test_auto_authenticate_on_get_companies(self, connector: AvastConnector) -> None:
        """Should auto-authenticate if token not yet set."""
        respx.post(_TOKEN_URL).mock(
            return_value=Response(200, json={"access_token": "auto_tok"})
        )
        respx.get("https://businesshub.avast.com/companies").mock(
            return_value=Response(200, json=[])
        )
        await connector.get_companies()
        assert connector._access_token == "auto_tok"
