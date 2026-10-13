"""Avast Business Hub connector.

Authentication: OAuth 2.0 client_credentials flow.
Token endpoint: POST https://businesshub.avast.com/oauth/token
"""

from typing import Any

import httpx
import structlog

from app.config import Settings, get_settings
from app.connectors.base import AbstractPlatformConnector
from app.models.company import CompanyRecord
from app.models.device import DeviceRecord
from app.services.normalizers import (
    normalize_company_name,
    normalize_hostname,
    normalize_mac,
    normalize_serial,
)

log = structlog.get_logger(__name__)

_TOKEN_URL = "https://businesshub.avast.com/oauth/token"
_BASE_URL = "https://businesshub.avast.com"


class AvastConnector(AbstractPlatformConnector):
    """Connector for Avast Business Hub."""

    platform_name = "avast"

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialise with injected or default settings."""
        self._settings = settings or get_settings()
        self._access_token: str = ""

    def check_config(self) -> list[str]:
        missing = []
        if not self._settings.avast_client_id:
            missing.append("AVAST_CLIENT_ID")
        if not self._settings.avast_client_secret:
            missing.append("AVAST_CLIENT_SECRET")
        return missing

    async def authenticate(self) -> None:
        """Obtain OAuth 2.0 bearer token using client_credentials grant."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._settings.avast_client_id,
                    "client_secret": self._settings.avast_client_secret,
                },
            )
            response.raise_for_status()
            body: dict[str, Any] = response.json()
            self._access_token = body["access_token"]
            log.info("avast.authenticated")

    def get_diagnostics(self) -> dict[str, Any]:
        """Return a diagnostic dict showing resolved config (for debugging)."""
        return {
            "base_url": _BASE_URL,
            "token_url": _TOKEN_URL,
            "companies_url": f"{_BASE_URL}/companies",
            "devices_url": f"{_BASE_URL}/devices",
            "client_id_set": bool(self._settings.avast_client_id),
            "client_secret_set": bool(self._settings.avast_client_secret),
            "has_token": bool(self._access_token),
        }

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def get_companies(self) -> list[CompanyRecord]:
        """Return all Avast companies as normalised :class:`CompanyRecord` objects."""
        if not self._access_token:
            await self.authenticate()

        async with httpx.AsyncClient(base_url=_BASE_URL) as client:
            response = await client.get("/companies", headers=self._auth_headers())
            response.raise_for_status()
            data: list[dict[str, Any]] = response.json()

        records: list[CompanyRecord] = []
        for item in data:
            records.append(
                CompanyRecord(
                    platform=self.platform_name,
                    platform_id=str(item.get("id", "")),
                    name=normalize_company_name(item.get("name", "")),
                    raw=item,
                )
            )
        log.info("avast.get_companies", count=len(records))
        return records

    async def get_devices(self) -> list[DeviceRecord]:
        """Return all Avast devices as normalised :class:`DeviceRecord` objects."""
        if not self._access_token:
            await self.authenticate()

        async with httpx.AsyncClient(base_url=_BASE_URL) as client:
            response = await client.get("/devices", headers=self._auth_headers())
            response.raise_for_status()
            data: list[dict[str, Any]] = response.json()

        records: list[DeviceRecord] = []
        for item in data:
            macs = [normalize_mac(m) for m in item.get("mac_addresses", [])]
            records.append(
                DeviceRecord(
                    platform=self.platform_name,
                    platform_id=str(item.get("id", "")),
                    hostname=normalize_hostname(item.get("hostname", "")),
                    serial_number=normalize_serial(item.get("serial_number", "")),
                    mac_addresses=[m for m in macs if m],
                    company_platform_id=str(item.get("company_id", "")),
                    raw=item,
                )
            )
        log.info("avast.get_devices", count=len(records))
        return records
