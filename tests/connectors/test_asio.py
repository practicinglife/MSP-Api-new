"""Tests for Asio connector using respx HTTP mocking (no real network)."""

import time

import pytest
import respx
from httpx import Response

from app.config import Settings
from app.connectors.asio import AsioConnector, _extract_next_cursor

BASE = "https://openapi.service.itsupport247.net"  # NA region
TOKEN_URL = f"{BASE}/v1/token"
COMPANIES_URL = f"{BASE}/api/platform/v1/company/companies"


@pytest.fixture
def asio_settings() -> Settings:
    return Settings(
        asio_client_id="asio-client-id",
        asio_client_secret="asio-client-secret",
        asio_region="NA",
    )


@pytest.fixture
def connector(asio_settings: Settings) -> AsioConnector:
    return AsioConnector(settings=asio_settings)


def _signed_in(connector: AsioConnector) -> AsioConnector:
    """A connector holding a still-valid token, so no token request is made."""
    connector._access_token = "asio_tok"
    connector._token_expires_at = time.time() + 3600
    return connector


class TestExtractNextCursor:
    """Tests for Link header cursor extraction helper."""

    def test_extracts_cursor(self) -> None:
        header = f'<{COMPANIES_URL}?cursor=abc123>; rel="next"'
        assert _extract_next_cursor(header) == "abc123"

    def test_returns_none_when_no_next(self) -> None:
        header = f'<{COMPANIES_URL}>; rel="prev"'
        assert _extract_next_cursor(header) is None

    def test_returns_none_for_empty(self) -> None:
        assert _extract_next_cursor("") is None


class TestAsioConnector:
    """Tests for AsioConnector."""

    @respx.mock
    async def test_json_body_auth(self, connector: AsioConnector) -> None:
        """Should POST client credentials as a JSON body (not form-encoded) to /v1/token."""
        token_route = respx.post(TOKEN_URL).mock(
            return_value=Response(200, json={"access_token": "asio_tok", "expires_in": 3600})
        )
        respx.get(COMPANIES_URL).mock(return_value=Response(200, json=[]))
        await connector.get_companies()
        request = token_route.calls[0].request
        assert "application/json" in request.headers.get("content-type", "")
        assert b'"grant_type": "client_credentials"' in request.content or b'"grant_type":"client_credentials"' in request.content

    @respx.mock
    async def test_paginated_companies_via_link_header(self, connector: AsioConnector) -> None:
        """Should follow Link header cursor pagination."""
        _signed_in(connector)
        route = respx.get(COMPANIES_URL)
        route.side_effect = [
            Response(200, json=[{"id": "c1", "name": "Company One"}],
                     headers={"Link": f'<{COMPANIES_URL}?cursor=page2>; rel="next"'}),
            Response(200, json=[{"id": "c2", "name": "Company Two"}]),
        ]
        companies = await connector.get_companies()
        assert len(companies) == 2
        assert route.calls[1].request.url.params["cursor"] == "page2"

    @respx.mock
    async def test_429_retry(self, connector: AsioConnector, monkeypatch: pytest.MonkeyPatch) -> None:
        """Should retry on HTTP 429 and eventually succeed (backoff sleep skipped in tests)."""
        import asyncio

        async def _no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        _signed_in(connector)
        route = respx.get(COMPANIES_URL)
        route.side_effect = [
            Response(429, json={"error": "rate_limited"}),
            Response(200, json=[{"id": "c1", "name": "Company One"}]),
        ]
        companies = await connector.get_companies()
        assert len(companies) == 1 and route.call_count == 2
