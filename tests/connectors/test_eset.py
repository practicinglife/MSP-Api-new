"""Tests for the ESET MSP Administrator 2 connector using respx HTTP mocking (no real network)."""

import json

import pytest
import respx
from httpx import Response

from app.config import Settings
from app.connectors import eset as eset_module
from app.connectors.eset import EsetConnector

BASE = "https://mspapi.eset.com"


@pytest.fixture
def eset_settings() -> Settings:
    return Settings(eset_username="user@example.com", eset_password="secret", eset_base_url=BASE)


@pytest.fixture
def connector(eset_settings: Settings) -> EsetConnector:
    return EsetConnector(settings=eset_settings)


class TestEsetConnector:
    """Tests for EsetConnector."""

    @respx.mock
    async def test_authenticate(self, connector: EsetConnector) -> None:
        """Should POST the username and password to /api/Token/Get and keep the tokens."""
        route = respx.post(f"{BASE}/api/Token/Get").mock(
            return_value=Response(200, json={"accessToken": "bearer_xyz", "refreshToken": "r1"}))
        await connector.authenticate()
        assert connector._access_token == "bearer_xyz" and connector._refresh_token == "r1"
        assert json.loads(route.calls[0].request.content) == {"username": "user@example.com", "password": "secret"}

    @respx.mock
    async def test_authenticate_retries_server_errors(
        self, connector: EsetConnector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 5xx from the token endpoint is retried (backoff skipped in tests)."""
        monkeypatch.setattr(eset_module, "_AUTH_RETRY_DELAY", 0.0)
        route = respx.post(f"{BASE}/api/Token/Get")
        route.side_effect = [Response(503), Response(200, json={"accessToken": "ok"})]
        await connector.authenticate()
        assert connector._access_token == "ok" and route.call_count == 2

    @respx.mock
    async def test_get_companies(self, connector: EsetConnector, monkeypatch: pytest.MonkeyPatch) -> None:
        """Should find our company via /api/User/Current, then page through /api/Company/Children."""
        monkeypatch.setattr(eset_module, "_PAGE_SIZE", 2)
        connector._access_token = "bearer_xyz"
        respx.get(f"{BASE}/api/User/Current").mock(return_value=Response(200, json={"companyId": "root-guid"}))
        children = respx.post(f"{BASE}/api/Company/Children")
        children.side_effect = [
            Response(200, json={"companies": [{"companyId": "c1", "name": "Child Company Ltd"},
                                              {"companyId": "c2", "name": "Second Company"}]}),
            Response(200, json=[{"publicId": "c3", "Name": "Third Company"}]),
        ]
        companies = await connector.get_companies()
        assert [c.platform_id for c in companies] == ["c1", "c2", "c3"]
        bodies = [json.loads(c.request.content) for c in children.calls]
        assert bodies == [{"companyId": "root-guid", "skip": 0, "take": 2},
                          {"companyId": "root-guid", "skip": 2, "take": 2}]
        assert children.calls[0].request.headers["Authorization"] == "Bearer bearer_xyz"

    @respx.mock
    async def test_get_companies_reauthenticates_on_401(self, connector: EsetConnector) -> None:
        """A rejected (expired) token is replaced once, then the call is repeated."""
        connector._access_token = "stale"
        respx.post(f"{BASE}/api/Token/Get").mock(return_value=Response(200, json={"accessToken": "fresh"}))
        current = respx.get(f"{BASE}/api/User/Current")
        current.side_effect = [Response(401), Response(200, json={"companyId": "root-guid"})]
        respx.post(f"{BASE}/api/Company/Children").mock(
            return_value=Response(200, json=[{"companyId": "c1", "name": "Child Company Ltd"}]))
        companies = await connector.get_companies()
        assert [c.platform_id for c in companies] == ["c1"]
        assert current.calls[1].request.headers["Authorization"] == "Bearer fresh"

    async def test_get_devices_not_supported(self, connector: EsetConnector) -> None:
        """EMA2 is a license portal without a device endpoint: no devices, and no API call."""
        assert await connector.get_devices() == []
