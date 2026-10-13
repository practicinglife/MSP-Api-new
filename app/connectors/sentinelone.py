"""SentinelOne connector.

Authentication: Authorization: ApiToken <token>
Endpoints:
  - GET /web/api/v2.1/agents  (cursor-paginated)
  - GET /web/api/v2.1/sites
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

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class SentinelOneConnector(AbstractPlatformConnector):
    """Connector for SentinelOne EDR platform."""

    platform_name = "sentinelone"
    supports_scan = True
    supports_alerts = True
    supports_offboard = True
    supports_company_create = True

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialise with injected or default settings."""
        self._settings = settings or get_settings()

    def check_config(self) -> list[str]:
        missing = []
        if not self._settings.s1_base_url:
            missing.append("S1_BASE_URL")
        if not self._settings.s1_api_token:
            missing.append("S1_API_TOKEN")
        return missing

    def _base_url(self) -> str:
        return self._settings.s1_base_url.rstrip("/")

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"ApiToken {self._settings.s1_api_token}"}

    async def authenticate(self) -> None:
        """SentinelOne uses a static API token — no separate auth step."""
        log.info("sentinelone.authenticate.noop")

    def get_diagnostics(self) -> dict[str, Any]:
        """Return a diagnostic dict showing resolved config (for debugging)."""
        return {
            "base_url": self._base_url(),
            "sites_url": f"{self._base_url()}/web/api/v2.1/sites",
            "agents_url": f"{self._base_url()}/web/api/v2.1/agents",
            "threats_url": f"{self._base_url()}/web/api/v2.1/threats",
            "api_token_set": bool(self._settings.s1_api_token),
            "supports_scan": self.supports_scan,
            "supports_alerts": self.supports_alerts,
        }

    async def get_companies(self) -> list[CompanyRecord]:
        """Return all SentinelOne sites as company records."""
        records: list[CompanyRecord] = []
        cursor: str | None = None

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            while True:
                params: dict[str, Any] = {"limit": 200}
                if cursor:
                    params["cursor"] = cursor
                response = await client.get(
                    f"{self._base_url()}/web/api/v2.1/sites",
                    headers=self._auth_headers(),
                    params=params,
                )
                response.raise_for_status()
                body: dict[str, Any] = response.json()
                sites: list[dict[str, Any]] = body.get("data", {}).get("sites", [])
                for site in sites:
                    records.append(
                        CompanyRecord(
                            platform=self.platform_name,
                            platform_id=str(site.get("id", "")),
                            name=normalize_company_name(site.get("name", "")),
                            raw=site,
                        )
                    )
                pagination = body.get("pagination", {})
                cursor = pagination.get("nextCursor")
                if not cursor:
                    break

        log.info("sentinelone.get_companies", count=len(records))
        return records

    async def get_device_ids(self) -> list[dict[str, str]]:
        """Lightweight ID probe — fetches only id, computerName, siteName, updatedAt.

        Uses S1 field selection to return ~5x less data than get_devices().
        """
        stubs: list[dict[str, str]] = []
        cursor: str | None = None
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            while True:
                params: dict[str, Any] = {
                    "limit": 1000,
                    "fields": "id,computerName,siteName,updatedAt",
                }
                if cursor:
                    params["cursor"] = cursor
                response = await client.get(
                    f"{self._base_url()}/web/api/v2.1/agents",
                    headers=self._auth_headers(),
                    params=params,
                )
                response.raise_for_status()
                body = response.json()
                for agent in body.get("data", []):
                    stubs.append({
                        "platform_id":  str(agent.get("id", "")),
                        "company_name": agent.get("siteName", ""),
                        "hostname":     agent.get("computerName", ""),
                        "updated_at":   agent.get("updatedAt", ""),
                    })
                cursor = body.get("pagination", {}).get("nextCursor")
                if not cursor:
                    break
        log.info("sentinelone.get_device_ids", count=len(stubs))
        return stubs

    async def get_devices(self) -> list[DeviceRecord]:
        """Return all SentinelOne agents with cursor-based pagination."""
        records: list[DeviceRecord] = []
        cursor: str | None = None

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            while True:
                params: dict[str, Any] = {"limit": 200}
                if cursor:
                    params["cursor"] = cursor
                response = await client.get(
                    f"{self._base_url()}/web/api/v2.1/agents",
                    headers=self._auth_headers(),
                    params=params,
                )
                response.raise_for_status()
                body: dict[str, Any] = response.json()
                agents: list[dict[str, Any]] = body.get("data", [])
                for agent in agents:
                    nics: list[dict[str, Any]] = agent.get("networkInterfaces", [])
                    macs = [normalize_mac(n.get("physical", "")) for n in nics if n.get("physical")]
                    # siteName is critical for company matching in the reconciler
                    site_name_raw = agent.get("siteName", "")
                    records.append(
                        DeviceRecord(
                            platform=self.platform_name,
                            platform_id=str(agent.get("id", "")),
                            hostname=normalize_hostname(agent.get("computerName", "")),
                            serial_number=normalize_serial(agent.get("serialNumber", "")),
                            mac_addresses=[m for m in macs if m],
                            company_platform_id=str(agent.get("siteId", "")),
                            company_name=site_name_raw,
                            site_name=site_name_raw,
                            raw=agent,
                        )
                    )
                pagination = body.get("pagination", {})
                cursor = pagination.get("nextCursor")
                if not cursor:
                    break

        log.info("sentinelone.get_devices", count=len(records))
        return records

    # --- Action capabilities ---

    async def initiate_scan(self, device_ids: list[str]) -> dict[str, Any] | None:
        """Trigger a Full Disk Scan on specified SentinelOne agent(s)."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self._base_url()}/web/api/v2.1/agents/actions/initiate-scan",
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                json={
                    "filter": {"ids": device_ids},
                    "data": {},
                },
            )
            response.raise_for_status()
            body = response.json()
            affected = body.get("data", {}).get("affected", 0)
            log.info("sentinelone.scan_initiated", device_count=len(device_ids), affected=affected)
            return {"affected": affected, "device_ids": device_ids}

    async def get_alerts(
        self, *, limit: int = 50, severity: str | None = None
    ) -> list[dict[str, Any]] | None:
        """Return recent SentinelOne threats."""
        params: dict[str, Any] = {"limit": limit, "sortBy": "createdAt", "sortOrder": "desc"}
        if severity:
            # SentinelOne uses confidenceLevel or analystVerdict
            params["analystVerdicts"] = severity

        results: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self._base_url()}/web/api/v2.1/threats",
                headers=self._auth_headers(),
                params=params,
            )
            response.raise_for_status()
            body = response.json()
            threats: list[dict[str, Any]] = body.get("data", [])
            for t in threats:
                agent_info = t.get("agentRealtimeInfo", {})
                threat_info = t.get("threatInfo", {})
                results.append({
                    "alert_id": str(t.get("id", "")),
                    "severity": threat_info.get("confidenceLevel", "unknown"),
                    "title": threat_info.get("threatName", ""),
                    "description": threat_info.get("classification", ""),
                    "device_hostname": agent_info.get("agentComputerName", ""),
                    "device_id": str(agent_info.get("agentId", "")),
                    "company_name": agent_info.get("siteName", ""),
                    "status": threat_info.get("mitigationStatus", ""),
                    "created_at": threat_info.get("createdAt", ""),
                    "raw": t,
                })
        log.info("sentinelone.get_alerts", count=len(results))
        return results

    # --- Remediation capabilities ---

    async def offboard_device(
        self, device_ids: list[str], **kwargs: Any
    ) -> dict[str, Any] | None:
        """Decommission SentinelOne agents.

        Uses POST /web/api/v2.1/agents/actions/decommission.
        This removes the agent from the console and uninstalls it from the
        endpoint (if reachable).
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self._base_url()}/web/api/v2.1/agents/actions/decommission",
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                json={"filter": {"ids": device_ids}},
            )
            response.raise_for_status()
            body = response.json()
            affected = body.get("data", {}).get("affected", 0)
            log.info("sentinelone.offboard", device_count=len(device_ids), affected=affected)
            return {"affected": affected, "device_ids": device_ids}

    async def uninstall_agent(
        self, device_ids: list[str], **kwargs: Any
    ) -> dict[str, Any] | None:
        """Uninstall SentinelOne agent from endpoints.

        Uses POST /web/api/v2.1/agents/actions/uninstall.
        The agent will self-remove on next check-in.
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self._base_url()}/web/api/v2.1/agents/actions/uninstall",
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                json={"filter": {"ids": device_ids}},
            )
            response.raise_for_status()
            body = response.json()
            affected = body.get("data", {}).get("affected", 0)
            log.info("sentinelone.uninstall", device_count=len(device_ids), affected=affected)
            return {"affected": affected, "device_ids": device_ids}

    async def get_packages(
        self, *, site_id: str | None = None, os_type: str = "windows"
    ) -> list[dict[str, Any]]:
        """Return available S1 agent packages for deployment.

        Uses GET /web/api/v2.1/update/agent/packages.
        """
        params: dict[str, Any] = {
            "sortBy": "createdAt",
            "sortOrder": "desc",
            "limit": 10,
            "fileExtension": ".exe",
            "osTypes": os_type,
            "packageTypes": "AgentAndRanger",
            "status": "ga",
        }
        if site_id:
            params["siteIds"] = site_id

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self._base_url()}/web/api/v2.1/update/agent/packages",
                headers=self._auth_headers(),
                params=params,
            )
            response.raise_for_status()
            body = response.json()
            packages = body.get("data", [])
            log.info("sentinelone.get_packages", count=len(packages))
            return [
                {
                    "id": str(p.get("id", "")),
                    "version": p.get("version", ""),
                    "os_type": p.get("osType", ""),
                    "file_name": p.get("fileName", ""),
                    "download_link": p.get("link", ""),
                    "status": p.get("status", ""),
                    "created_at": p.get("createdAt", ""),
                }
                for p in packages
            ]

    async def create_company_on_platform(
        self, name: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Create a new SentinelOne site (company equivalent).

        Uses POST /web/api/v2.1/sites.
        Requires ``account_id`` in kwargs (the parent account).
        """
        account_id = kwargs.get("account_id", "")
        if not account_id:
            log.error("sentinelone.create_site.no_account_id")
            return {"error": "account_id is required to create a SentinelOne site"}

        payload: dict[str, Any] = {
            "data": {
                "name": name,
                "accountId": account_id,
                "siteType": "Paid",
                "suite": "Complete",
                "totalLicenses": kwargs.get("licenses", 0),
                "unlimitedLicenses": kwargs.get("licenses", 0) == 0,
            }
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self._base_url()}/web/api/v2.1/sites",
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            site_data = body.get("data", {})
            log.info("sentinelone.create_site", name=name, site_id=site_data.get("id"))
            return {
                "platform": self.platform_name,
                "id": str(site_data.get("id", "")),
                "name": site_data.get("name", name),
                "raw": site_data,
            }
