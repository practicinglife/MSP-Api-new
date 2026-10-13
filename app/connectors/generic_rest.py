"""Generic REST API connector with selectable authentication methods.

Supports adding any REST API platform with configurable auth:
- API Key (custom header name + value)
- Basic Auth (username:password → Base64 Authorization header)
- Bearer Token (Authorization: Bearer <token>)
- Custom Header (user-defined header name + value)

The connector makes a GET request to a devices endpoint and a companies
endpoint, expecting JSON arrays in return.
"""

import base64
from typing import Any

import httpx
import structlog

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


class GenericRestConnector(AbstractPlatformConnector):
    """Connector for any REST API with configurable authentication.

    The connector reads its configuration from a Settings object containing
    generic_* prefixed fields, or from a credentials dict passed to
    ``build_connector``.
    """

    def __init__(
        self,
        *,
        platform_label: str = "generic_rest",
        base_url: str = "",
        auth_method: str = "none",
        auth_header_name: str = "",
        auth_header_value: str = "",
        auth_username: str = "",
        auth_password: str = "",
        auth_token: str = "",
        companies_endpoint: str = "",
        devices_endpoint: str = "",
        company_name_field: str = "name",
        device_hostname_field: str = "hostname",
        device_serial_field: str = "serial_number",
        device_mac_field: str = "mac_address",
        device_company_field: str = "company",
    ) -> None:
        self.platform_name = platform_label
        self._base = base_url.rstrip("/") if base_url else ""
        self._auth_method = auth_method  # none, api_key, basic, bearer, custom_header
        self._auth_header_name = auth_header_name
        self._auth_header_value = auth_header_value
        self._auth_username = auth_username
        self._auth_password = auth_password
        self._auth_token = auth_token
        self._companies_endpoint = companies_endpoint
        self._devices_endpoint = devices_endpoint
        self._company_name_field = company_name_field
        self._device_hostname_field = device_hostname_field
        self._device_serial_field = device_serial_field
        self._device_mac_field = device_mac_field
        self._device_company_field = device_company_field
        self._client: httpx.AsyncClient | None = None
        self._last_status: int = 0

    # ── configuration ──────────────────────────────────────────────

    def check_config(self) -> list[str]:
        missing: list[str] = []
        if not self._base:
            missing.append("base_url")
        if self._auth_method == "api_key" and not self._auth_header_value:
            missing.append("auth_header_value")
        if self._auth_method == "basic" and not (self._auth_username and self._auth_password):
            missing.append("auth_username / auth_password")
        if self._auth_method == "bearer" and not self._auth_token:
            missing.append("auth_token")
        if self._auth_method == "custom_header" and not (self._auth_header_name and self._auth_header_value):
            missing.append("auth_header_name / auth_header_value")
        return missing

    def _build_headers(self) -> dict[str, str]:
        """Build request headers based on the selected auth method."""
        headers: dict[str, str] = {"Content-Type": "application/json"}

        if self._auth_method == "api_key":
            header_name = self._auth_header_name or "X-API-Key"
            headers[header_name] = self._auth_header_value

        elif self._auth_method == "basic":
            creds = f"{self._auth_username}:{self._auth_password}"
            encoded = base64.b64encode(creds.encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"

        elif self._auth_method == "bearer":
            headers["Authorization"] = f"Bearer {self._auth_token}"

        elif self._auth_method == "custom_header":
            if self._auth_header_name:
                headers[self._auth_header_name] = self._auth_header_value

        return headers

    # Same interface as the built-in connectors, used by the proxy and API explorer.
    def _base_url(self) -> str:
        return self._base

    def _auth_headers(self) -> dict[str, str]:
        return self._build_headers()

    # ── HTTP client ────────────────────────────────────────────────

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=60.0,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── authenticate ───────────────────────────────────────────────

    async def authenticate(self) -> None:
        """Validate connection by hitting the base URL or devices endpoint."""
        client = await self._ensure_client()
        test_url = f"{self._base}/{self._devices_endpoint.lstrip('/')}" if self._devices_endpoint else self._base
        try:
            response = await client.get(test_url, headers=self._build_headers())
            self._last_status = response.status_code
            response.raise_for_status()
            log.info("generic_rest.authenticate.ok", platform=self.platform_name, status=response.status_code)
        except httpx.HTTPStatusError as exc:
            log.error("generic_rest.authenticate.failed", platform=self.platform_name, status=exc.response.status_code)
            raise RuntimeError(f"{self.platform_name}: auth failed — HTTP {exc.response.status_code}") from exc
        except httpx.ConnectError as exc:
            log.error("generic_rest.authenticate.connect_error", platform=self.platform_name, error=str(exc))
            raise RuntimeError(f"{self.platform_name}: cannot connect — {exc}") from exc

    # ── data fetching ──────────────────────────────────────────────

    async def _fetch_json(self, endpoint: str) -> list[dict[str, Any]]:
        """GET a JSON endpoint and return a list of dicts."""
        if not endpoint:
            return []
        client = await self._ensure_client()
        url = f"{self._base}/{endpoint.lstrip('/')}"
        response = await client.get(url, headers=self._build_headers())
        self._last_status = response.status_code
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Try common wrapper patterns
            for key in ("data", "results", "items", "records", "value"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            return [data]
        return []

    async def get_companies(self) -> list[CompanyRecord]:
        """Fetch companies from the configured endpoint."""
        if not self._companies_endpoint:
            return []
        items = await self._fetch_json(self._companies_endpoint)
        seen: set[str] = set()
        records: list[CompanyRecord] = []
        for item in items:
            name = str(item.get(self._company_name_field, ""))
            norm = normalize_company_name(name)
            if norm and norm not in seen:
                seen.add(norm)
                records.append(CompanyRecord(
                    platform=self.platform_name,
                    platform_id=norm,
                    name=norm,
                    raw=item,
                ))
        log.info("generic_rest.get_companies", platform=self.platform_name, count=len(records))
        return records

    async def get_devices(self) -> list[DeviceRecord]:
        """Fetch devices from the configured endpoint."""
        if not self._devices_endpoint:
            return []
        items = await self._fetch_json(self._devices_endpoint)
        records: list[DeviceRecord] = []
        for item in items:
            hostname = str(item.get(self._device_hostname_field, ""))
            serial = str(item.get(self._device_serial_field, ""))
            company = str(item.get(self._device_company_field, ""))

            # Handle MAC addresses (string or list)
            mac_raw = item.get(self._device_mac_field, "")
            if isinstance(mac_raw, list):
                macs = [normalize_mac(str(m)) for m in mac_raw if m]
            elif isinstance(mac_raw, str) and mac_raw:
                macs = [normalize_mac(m.strip()) for m in mac_raw.replace(";", ",").split(",") if m.strip()]
            else:
                macs = []

            records.append(DeviceRecord(
                platform=self.platform_name,
                platform_id=str(item.get("id", "")) or hostname,
                hostname=normalize_hostname(hostname),
                serial_number=normalize_serial(serial),
                mac_addresses=[m for m in macs if m],
                company_platform_id=normalize_company_name(company),
                company_name=normalize_company_name(company),
                raw=item,
            ))
        log.info("generic_rest.get_devices", platform=self.platform_name, count=len(records))
        return records

    def get_diagnostics(self) -> dict[str, Any]:
        return {
            "base_url": self._base,
            "auth_method": self._auth_method,
            "companies_endpoint": self._companies_endpoint,
            "devices_endpoint": self._devices_endpoint,
            "last_status": self._last_status,
        }
