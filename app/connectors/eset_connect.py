"""ESET Connect (ESET Protect Cloud) connector — per-device AV details.

Authentication: OAuth 2.0 Resource Owner Password Credentials grant.
  POST {auth_url}/oauth/token  (form-encoded, grant_type=password)
  Returns Bearer access_token + refresh_token.

Device Management API:
  GET  {base_url}/v1/devices          → paginated list (pageToken, pageSize up to 1000)
  GET  {base_url}/v1/devices/{uuid}   → single device detail
  GET  {base_url}/v1/device_groups    → list all device groups
  GET  {base_url}/v1/installers       → list Live Installer packages (per-company
                                        deviceEnrollment group + downloadUrl)

Key schema (v1Device):
  - uuid, displayName, operatingSystem
  - activeProducts  [{name, activateDate, validityDate}]
  - deployedComponents [{displayName, version {name, major, minor, patch}}]
  - lastSyncTime, parentGroupUuid, primaryLocalIpAddress, publicIpAddress

This connector is a **data enrichment source** — it provides per-device
ESET product versions, not a general device inventory.
"""

from typing import Any

import httpx
import structlog

from app.config import Settings, get_settings

log = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_PAGE_SIZE = 1000


class EsetConnectConnector:
    """Connector for the ESET Connect (Protect Cloud) Device Management API."""

    platform_name = "eset_connect"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._access_token: str = ""
        self._refresh_token: str = ""

    def check_config(self) -> list[str]:
        """Return list of missing configuration field names."""
        missing: list[str] = []
        if not self._settings.eset_connect_username:
            missing.append("ESET_CONNECT_USERNAME")
        if not self._settings.eset_connect_password:
            missing.append("ESET_CONNECT_PASSWORD")
        return missing

    async def authenticate(self) -> bool:
        """Authenticate via OAuth 2.0 password grant and store Bearer token."""
        missing = self.check_config()
        if missing:
            log.warning("eset_connect.auth.missing_config", missing=missing)
            return False

        auth_url = self._settings.eset_connect_auth_url.rstrip("/")
        token_endpoint = f"{auth_url}/oauth/token"

        payload = {
            "grant_type": "password",
            "username": self._settings.eset_connect_username,
            "password": self._settings.eset_connect_password,
        }

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    token_endpoint,
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                resp.raise_for_status()
                data = resp.json()
                self._access_token = data.get("access_token", "")
                self._refresh_token = data.get("refresh_token", "")
                log.info(
                    "eset_connect.auth.success",
                    token_type=data.get("token_type", ""),
                    expires_in=data.get("expires_in", ""),
                )
                return bool(self._access_token)
        except httpx.HTTPStatusError as exc:
            log.error(
                "eset_connect.auth.http_error",
                status=exc.response.status_code,
                body=exc.response.text[:500],
            )
            return False
        except Exception as exc:
            log.error("eset_connect.auth.error", error=str(exc))
            return False

    def _auth_headers(self) -> dict[str, str]:
        """Return Authorization header dict."""
        return {"Authorization": f"Bearer {self._access_token}"}

    async def get_devices(self) -> list[dict[str, Any]]:
        """Fetch all devices from ESET Connect with pageToken pagination.

        Returns raw v1Device dicts including deployedComponents and
        activeProducts for version extraction.
        """
        if not self._access_token:
            authenticated = await self.authenticate()
            if not authenticated:
                return []

        base_url = self._settings.eset_connect_base_url.rstrip("/")
        all_devices: list[dict[str, Any]] = []
        page_token: str | None = None

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            while True:
                params: dict[str, Any] = {"pageSize": _PAGE_SIZE}
                if page_token:
                    params["pageToken"] = page_token

                try:
                    resp = await client.get(
                        f"{base_url}/v1/devices",
                        headers=self._auth_headers(),
                        params=params,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except httpx.HTTPStatusError as exc:
                    log.error(
                        "eset_connect.devices.http_error",
                        status=exc.response.status_code,
                        body=exc.response.text[:500],
                        page_token=page_token,
                    )
                    break
                except Exception as exc:
                    log.error("eset_connect.devices.error", error=str(exc))
                    break

                devices = data.get("devices", [])
                all_devices.extend(devices)

                page_token = data.get("nextPageToken")
                if not page_token:
                    break

                log.info(
                    "eset_connect.devices.page",
                    fetched=len(devices),
                    total_so_far=len(all_devices),
                )

        log.info("eset_connect.devices.done", total=len(all_devices))
        return all_devices

    async def get_device_groups(self) -> list[dict[str, Any]]:
        """Fetch all device groups from ESET Connect."""
        if not self._access_token:
            authenticated = await self.authenticate()
            if not authenticated:
                return []

        base_url = self._settings.eset_connect_base_url.rstrip("/")

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f"{base_url}/v1/device_groups",
                    headers=self._auth_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                groups = data.get("deviceGroups", data.get("groups", []))
                log.info("eset_connect.groups.done", total=len(groups))
                return groups
        except httpx.HTTPStatusError as exc:
            log.error(
                "eset_connect.groups.http_error",
                status=exc.response.status_code,
                body=exc.response.text[:500],
            )
            return []
        except Exception as exc:
            log.error("eset_connect.groups.error", error=str(exc))
            return []

    async def get_installers(self) -> list[dict[str, Any]]:
        """Fetch all Live Installer packages from ESET Connect.

        Each installer record includes:
          - ``uuid``, ``displayName``
          - ``deviceEnrollment.groupDisplayName`` — the company/group it enrolls into
          - ``deviceEnrollment.securityGroupUuid``
          - ``activatedProducts`` — product + subscription/pool info
          - ``downloadUrl`` — the Live Installer download link
            (https://redirector.eset.systems/li-handler/?uuid=epi_win-...)
          - ``usable`` (bool) and ``expireTime`` — validity indicators

        These are the same per-company installer links shown in the
        ESET Protect console's "Installers" section.
        """
        if not self._access_token:
            authenticated = await self.authenticate()
            if not authenticated:
                return []

        base_url = self._settings.eset_connect_base_url.rstrip("/")
        all_installers: list[dict[str, Any]] = []
        page_token: str | None = None

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            while True:
                params: dict[str, Any] = {"pageSize": _PAGE_SIZE}
                if page_token:
                    params["pageToken"] = page_token

                try:
                    resp = await client.get(
                        f"{base_url}/v1/installers",
                        headers=self._auth_headers(),
                        params=params,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except httpx.HTTPStatusError as exc:
                    log.error(
                        "eset_connect.installers.http_error",
                        status=exc.response.status_code,
                        body=exc.response.text[:500],
                    )
                    break
                except Exception as exc:
                    log.error("eset_connect.installers.error", error=str(exc))
                    break

                all_installers.extend(data.get("installers", []))
                page_token = data.get("nextPageToken")
                if not page_token:
                    break

        log.info("eset_connect.installers.done", total=len(all_installers))
        return all_installers

    @staticmethod
    def extract_eset_versions(device: dict[str, Any]) -> list[dict[str, str]]:
        """Extract ESET product names and versions from a v1Device dict.

        Parses ``deployedComponents`` for product display names and version
        strings (``version.name`` like ``"12.1.2057.3"``).

        Returns a list of dicts with keys: product, version.
        """
        results: list[dict[str, str]] = []

        for component in device.get("deployedComponents", []):
            display_name = component.get("displayName", "")
            version_obj = component.get("version", {})
            version_str = ""
            if isinstance(version_obj, dict):
                version_str = version_obj.get("name", "")
                if not version_str:
                    parts = []
                    for key in ("major", "minor", "patch"):
                        val = version_obj.get(key)
                        if val is not None:
                            parts.append(str(val))
                    if parts:
                        version_str = ".".join(parts)

            if display_name:
                results.append({
                    "product": display_name,
                    "version": version_str,
                })

        return results

    @staticmethod
    def extract_active_products(device: dict[str, Any]) -> list[dict[str, str]]:
        """Extract active product license info from a v1Device dict.

        Parses ``activeProducts`` for product name, activation date,
        and validity date.
        """
        results: list[dict[str, str]] = []
        for product in device.get("activeProducts", []):
            results.append({
                "name": product.get("name", ""),
                "activate_date": product.get("activateDate", ""),
                "validity_date": product.get("validityDate", ""),
            })
        return results
