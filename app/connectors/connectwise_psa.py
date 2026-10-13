"""ConnectWise PSA connector.

Authentication: HTTP Basic (Base64(companyId+publicKey:privateKey)) + clientId header.
Endpoints: /company/companies, /company/configurations.
"""

import base64
from typing import Any

import httpx
import structlog

from app.config import Settings, get_settings
from app.connectors.base import AbstractPlatformConnector
from app.models.company import CompanyRecord
from app.models.device import DeviceRecord
from app.org_profile import (
    PSA_AV_BILLING_AGREEMENT_TYPE,
    PSA_DEVICE_TYPES,
    PSA_MANAGED_AGREEMENT_TYPE,
    PSA_S1_AGREEMENT_TYPE,
)
from app.services.normalizers import (
    normalize_company_name,
    normalize_hostname,
    normalize_mac,
    normalize_serial,
)

log = structlog.get_logger(__name__)

_PAGE_SIZE = 1000
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class ConnectWisePsaConnector(AbstractPlatformConnector):
    """Connector for ConnectWise PSA (Manage)."""

    platform_name = "connectwise_psa"
    supports_tickets = True
    supports_company_create = True

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialise with injected or default settings."""
        self._settings = settings or get_settings()

    def check_config(self) -> list[str]:
        missing = []
        if not self._settings.cw_company_id:
            missing.append("CW_COMPANY_ID")
        if not self._settings.cw_public_key:
            missing.append("CW_PUBLIC_KEY")
        if not self._settings.cw_private_key:
            missing.append("CW_PRIVATE_KEY")
        if not self._settings.cw_client_id:
            missing.append("CW_CLIENT_ID")
        return missing

    def _auth_headers(self) -> dict[str, str]:
        """Build Basic auth + clientId headers."""
        cred = (
            f"{self._settings.cw_company_id}+{self._settings.cw_public_key}"
            f":{self._settings.cw_private_key}"
        )
        encoded = base64.b64encode(cred.encode()).decode()
        return {
            "Authorization": f"Basic {encoded}",
            "clientId": self._settings.cw_client_id,
            "Content-Type": "application/json",
        }

    async def authenticate(self) -> None:
        """ConnectWise PSA uses per-request Basic auth — no separate token step."""
        log.info("connectwise_psa.authenticate.noop")

    def _base_url(self) -> str:
        return self._settings.cw_base_url.rstrip("/")

    async def _paginated_get(
        self, path: str, *, conditions: str = "",
    ) -> list[dict[str, Any]]:
        """Fetch all pages from a paginated ConnectWise endpoint."""
        results: list[dict[str, Any]] = []
        page = 1
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            while True:
                params: dict[str, Any] = {"pageSize": _PAGE_SIZE, "page": page}
                if conditions:
                    params["conditions"] = conditions
                response = await client.get(
                    f"{self._base_url()}{path}",
                    headers=self._auth_headers(),
                    params=params,
                )
                response.raise_for_status()
                batch: list[dict[str, Any]] = response.json()
                if not batch:
                    break
                results.extend(batch)
                if len(batch) < _PAGE_SIZE:
                    break
                page += 1
        return results

    async def _agreements_of_type(self, type_name: str, status: str, label: str) -> list[dict[str, Any]]:
        """Return non-cancelled agreements of one agreement *type* name and status.

        An empty ``type_name`` (setting not configured) returns ``[]`` without calling PSA.
        """
        if not type_name:
            return []
        safe_name = type_name.replace('"', '\\"')
        agreements = await self._paginated_get(
            "/finance/agreements",
            conditions=(
                f'type/name = "{safe_name}"'
                f' AND agreementStatus = "{status}"'
                " AND cancelledFlag = false"
            ),
        )
        log.info(f"connectwise_psa.{label}", type=type_name, count=len(agreements))
        return agreements

    async def _get_managed_agreements(self) -> list[dict[str, Any]]:
        """Return active managed-services agreements (type ``MSP_PSA_MANAGED_AGREEMENT_TYPE``)."""
        return await self._agreements_of_type(PSA_MANAGED_AGREEMENT_TYPE, "Active", "managed_agreements")

    async def _get_inactive_managed_agreements(self) -> list[dict[str, Any]]:
        """Return ALL inactive managed-services agreements.

        This is used to find early-added companies that have agreements
        scheduled for future activation (e.g. added before first of month).

        Fetches all inactive agreements of the type (typically a small
        set) rather than trying to filter by company IDs (which can create
        URLs that are too long when there are many companies).
        """
        return await self._agreements_of_type(
            PSA_MANAGED_AGREEMENT_TYPE, "Inactive", "inactive_managed_agreements",
        )

    async def _get_av_billing_agreements(self) -> list[dict[str, Any]]:
        """Return active AV-billing agreements (type ``MSP_PSA_AV_BILLING_AGREEMENT_TYPE``).

        This captures SentinelOne agreements billed under a separate AV agreement type.
        """
        return await self._agreements_of_type(PSA_AV_BILLING_AGREEMENT_TYPE, "Active", "av_billing_agreements")

    async def _get_sentinel_one_agreements(self) -> list[dict[str, Any]]:
        """Return active standalone SentinelOne agreements (type ``MSP_PSA_S1_AGREEMENT_TYPE``).

        These are companies WITHOUT a managed-services agreement that have standalone S1 agreements.
        """
        return await self._agreements_of_type(PSA_S1_AGREEMENT_TYPE, "Active", "sentinel_one_agreements")

    async def _get_managed_agreement_company_ids(self) -> set[int]:
        """Return company IDs that have an active managed-services agreement."""
        agreements = await self._get_managed_agreements()
        company_ids: set[int] = set()
        for ag in agreements:
            cid = ag.get("company", {}).get("id")
            if cid:
                company_ids.add(int(cid))
        log.info("connectwise_psa.managed_agreement_companies", count=len(company_ids))
        return company_ids

    async def get_company_av_map(self) -> dict[str, str]:
        """Return a mapping of normalised company name → assigned AV product.

        Inspects *additions* on all relevant active agreements (managed-services,
        AV-billing and standalone SentinelOne types).  Addition product descriptions containing
        ``eset`` map to ``"eset"`` and those containing ``sentinel`` map to ``"sentinelone"``.

        Returns dict values: ``"eset"``, ``"sentinelone"``, ``"both"``,
        or ``"none"`` (agreement exists but no AV addition found).
        """
        # Fetch managed-services, AV-billing and standalone SentinelOne agreements
        managed_agreements = await self._get_managed_agreements()
        av_billing_agreements = await self._get_av_billing_agreements()
        s1_agreements = await self._get_sentinel_one_agreements()
        agreements = managed_agreements + av_billing_agreements + s1_agreements

        # company_name → set of AV identifiers found
        av_sets: dict[str, set[str]] = {}

        async with httpx.AsyncClient(timeout=30.0) as client:
            for ag in agreements:
                ag_id = ag.get("id")
                company_ref = ag.get("company", {})
                company_name = normalize_company_name(company_ref.get("name", ""))
                if not ag_id or not company_name:
                    continue

                try:
                    response = await client.get(
                        f"{self._base_url()}/finance/agreements/{ag_id}/additions",
                        headers=self._auth_headers(),
                        params={"pageSize": 250},
                    )
                    response.raise_for_status()
                    additions: list[dict[str, Any]] = response.json()
                except Exception:  # noqa: BLE001
                    log.warning(
                        "connectwise_psa.additions_error",
                        agreement_id=ag_id,
                        company=company_name,
                    )
                    continue

                av_found: set[str] = av_sets.get(company_name, set())
                for addition in additions:
                    product = addition.get("product", {})
                    desc = (
                        (product.get("description") or "")
                        + " "
                        + (product.get("identifier") or "")
                    ).lower()
                    if "eset" in desc:
                        av_found.add("eset")
                    if "sentinel" in desc or "sentone" in desc:
                        av_found.add("sentinelone")
                av_sets[company_name] = av_found

        # Flatten to single label
        result: dict[str, str] = {}
        for company_name, avs in av_sets.items():
            if "eset" in avs and "sentinelone" in avs:
                result[company_name] = "both"
            elif "eset" in avs:
                result[company_name] = "eset"
            elif "sentinelone" in avs:
                result[company_name] = "sentinelone"
            else:
                result[company_name] = "none"

        log.info(
            "connectwise_psa.av_map",
            total=len(result),
            eset=sum(1 for v in result.values() if v in ("eset", "both")),
            sentinelone=sum(1 for v in result.values() if v in ("sentinelone", "both")),
            none=sum(1 for v in result.values() if v == "none"),
        )
        return result

    async def get_companies(self) -> list[CompanyRecord]:
        """Return ConnectWise PSA companies with managed-services or SentinelOne agreements.

        This filters to only managed companies (those with managed-services or SentinelOne agreements)
        to make cross-platform matching more relevant. Returns ~80 companies
        instead of 2800+ unfiltered companies.
        """
        # Get all companies
        data = await self._paginated_get("/company/companies")

        # Get managed company IDs (managed-services + SentinelOne). With no agreement types configured there is
        # nothing to filter on, so every company is returned (the same rule get_devices uses).
        filtering = bool(PSA_MANAGED_AGREEMENT_TYPE or PSA_S1_AGREEMENT_TYPE)
        managed_ids = await self.get_managed_company_ids() if filtering else set()

        records: list[CompanyRecord] = []
        for item in data:
            company_id = item.get("id")
            if company_id and (not filtering or int(company_id) in managed_ids):
                records.append(
                    CompanyRecord(
                        platform=self.platform_name,
                        platform_id=str(company_id),
                        name=normalize_company_name(item.get("name", "")),
                        raw=item,
                    )
                )
        log.info(
            "connectwise_psa.get_companies",
            total=len(data),
            filtered=len(records),
            managed_ids=len(managed_ids),
        )
        return records

    async def get_managed_company_ids(self) -> set[int]:
        """Return company IDs that have active managed-services OR SentinelOne agreements.

        This includes companies with a managed-services agreement and SentinelOne-only companies
        for comprehensive cross-platform matching and potential device licensing tracking.
        """
        # Companies with a managed-services agreement
        agreement_ids = await self._get_managed_agreement_company_ids()

        # Get Sentinel One agreement companies
        s1_agreements = await self._get_sentinel_one_agreements()
        s1_company_ids: set[int] = set()
        for ag in s1_agreements:
            cid = ag.get("company", {}).get("id")
            if cid:
                s1_company_ids.add(int(cid))

        # Combine both
        all_managed_ids = agreement_ids | s1_company_ids
        log.info(
            "connectwise_psa.managed_companies",
            managed_agreement=len(agreement_ids),
            sentinel_one=len(s1_company_ids),
            total=len(all_managed_ids),
        )
        return all_managed_ids

    async def get_managed_companies(self) -> list[CompanyRecord]:
        """Return companies that have active managed-services OR SentinelOne agreements.

        This filters the full company list to only managed companies,
        which is much more relevant for cross-platform matching than
        the full 2800+ company list.

        Includes managed-services and SentinelOne-only companies for device licensing tracking.
        """
        managed_ids = await self.get_managed_company_ids()
        all_companies = await self.get_companies()
        filtered = [c for c in all_companies if int(c.platform_id) in managed_ids]
        log.info(
            "connectwise_psa.get_managed_companies",
            total=len(all_companies),
            filtered=len(filtered),
        )
        return filtered

    # Configuration type names that count as managed devices (MSP_PSA_DEVICE_TYPES; empty = every type)
    _MANAGED_TYPES: set[str] = set(PSA_DEVICE_TYPES)

    async def get_device_ids(self) -> list[dict[str, str]]:
        """Lightweight ID probe — fetches only id, company, name, lastUpdated.

        ~10x faster than get_devices() — no custom fields, no MAC parsing.
        Used by smart reconcile to detect which companies changed.
        """
        data = await self._paginated_get(
            "/company/configurations",
            conditions='activeFlag=true',
        )
        return [
            {
                "platform_id":  str(item.get("id", "")),
                "company_name": (item.get("company") or {}).get("name", ""),
                "hostname":     item.get("name", ""),
                "updated_at":   item.get("lastUpdated", ""),
            }
            for item in data
            if item.get("id")
        ]

    async def get_devices(self) -> list[DeviceRecord]:
        """Return ConnectWise PSA configurations filtered to Managed Workstations/Servers.

        Extracts company and site references from each configuration to
        populate the hierarchical Company → Site → Device fields.
        The Asio Endpoint ID (UUID) is pulled from the "Endpoint ID"
        custom field to enable 1:1 cross-platform matching with Asio.

        Includes devices from companies with ACTIVE *or* INACTIVE managed-services agreements
        AND companies with Sentinel One agreements (standalone SentinelOne without managed services).
        """
        # Fetch company IDs with active managed-services agreements
        active_managed_ids = await self._get_managed_agreement_company_ids()

        # Fetch inactive agreements (all inactive managed-services agreements)
        inactive_agreements = await self._get_inactive_managed_agreements()
        inactive_managed_ids: set[int] = set()
        for ag in inactive_agreements:
            cid = ag.get("company", {}).get("id")
            if cid:
                inactive_managed_ids.add(int(cid))

        # Fetch Sentinel One agreement company IDs
        s1_agreements = await self._get_sentinel_one_agreements()
        s1_company_ids: set[int] = set()
        for ag in s1_agreements:
            cid = ag.get("company", {}).get("id")
            if cid:
                s1_company_ids.add(int(cid))

        # Combine active + inactive managed-services and SentinelOne company IDs
        all_managed_ids = active_managed_ids | inactive_managed_ids | s1_company_ids
        log.info(
            "connectwise_psa.managed_company_ids",
            active_managed=len(active_managed_ids),
            inactive_managed=len(inactive_managed_ids),
            sentinel_one=len(s1_company_ids),
            total=len(all_managed_ids),
        )

        data = await self._paginated_get("/company/configurations")
        records: list[DeviceRecord] = []
        skipped = 0
        skipped_inactive = 0
        skipped_unmanaged = 0
        for item in data:
            # Filter: only Managed Workstations and Managed Servers
            config_type = item.get("type", {})
            type_name = (config_type.get("name", "") if isinstance(config_type, dict) else "").lower()
            if self._MANAGED_TYPES and type_name not in self._MANAGED_TYPES:
                skipped += 1
                continue

            # Filter: only active configurations
            if not item.get("activeFlag", True):
                skipped_inactive += 1
                continue

            # Filter: only companies with an active/inactive managed-services OR SentinelOne agreement
            company_ref = item.get("company", {})
            company_id = company_ref.get("id")
            if all_managed_ids and company_id and int(company_id) not in all_managed_ids:
                skipped_unmanaged += 1
                continue

            macs = [normalize_mac(m) for m in item.get("macAddress", "").split(",") if m.strip()]
            site_ref = item.get("site", {})

            # Extract Endpoint ID (Asio UUID) from customFields
            endpoint_id = ""
            for cf in item.get("customFields") or []:
                caption = (cf.get("caption") or "").strip().lower()
                if caption == "endpoint id":
                    val = cf.get("value")
                    endpoint_id = str(val).strip() if val else ""
                    break

            records.append(
                DeviceRecord(
                    platform=self.platform_name,
                    platform_id=str(item.get("id", "")),
                    hostname=normalize_hostname(item.get("name", "")),
                    serial_number=normalize_serial(item.get("serialNumber", "")),
                    mac_addresses=[m for m in macs if m],
                    company_platform_id=str(company_ref.get("id", "")),
                    company_name=normalize_company_name(company_ref.get("name", "")),
                    site_id=str(site_ref.get("id", "")) if isinstance(site_ref, dict) else "",
                    site_name=site_ref.get("name", "") if isinstance(site_ref, dict) else "",
                    endpoint_id=endpoint_id,
                    raw=item,
                )
            )
        log.info(
            "connectwise_psa.get_devices",
            count=len(records),
            skipped_non_managed=skipped,
            skipped_inactive=skipped_inactive,
            skipped_unmanaged=skipped_unmanaged,
        )
        return records

    async def create_company_on_platform(
        self, name: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Create a new company in ConnectWise PSA.

        Uses POST /company/companies.
        """
        url = f"{self._base_url()}/company/companies"  # the base URL already ends in /apis/3.0

        payload: dict[str, Any] = {
            "identifier": name,  # Short identifier / code
            "name": name,        # Full company name
            "status": {"id": 1}, # Default to "Active" status
        }

        # Optional fields
        if kwargs.get("address"):
            payload["addressLine1"] = kwargs["address"]
        if kwargs.get("phone"):
            payload["phoneNumber"] = kwargs["phone"]

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    url,
                    headers=self._auth_headers(),
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
                log.info("connectwise_psa.create_company", name=name, id=body.get("id"))
                return {
                    "platform": self.platform_name,
                    "id": str(body.get("id", "")),
                    "name": body.get("name", name),
                    "identifier": body.get("identifier", name),
                    "raw": body,
                }
        except httpx.HTTPStatusError as exc:
            log.error(
                "connectwise_psa.create_company.error",
                status=exc.response.status_code,
                body=exc.response.text[:300],
            )
            return {"error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"}
        except Exception as exc:  # noqa: BLE001
            log.error("connectwise_psa.create_company.exception", error=str(exc))
            return {"error": str(exc)}

    # --- Action capabilities ---

    async def get_tickets(
        self, *, company_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]] | None:
        """Return recent ConnectWise PSA service tickets."""
        conditions = "closedFlag=false"
        if company_id:
            conditions += f" AND company/id={company_id}"
        params: dict[str, Any] = {
            "conditions": conditions,
            "pageSize": limit,
            "orderBy": "id desc",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self._base_url()}/service/tickets",
                headers=self._auth_headers(),
                params=params,
            )
            response.raise_for_status()
            data: list[dict[str, Any]] = response.json()

        results: list[dict[str, Any]] = []
        for t in data:
            company = t.get("company", {})
            priority = t.get("priority", {})
            status_ref = t.get("status", {})
            results.append({
                "ticket_id": str(t.get("id", "")),
                "summary": t.get("summary", ""),
                "status": status_ref.get("name", ""),
                "priority": priority.get("name", ""),
                "company_name": company.get("name", ""),
                "company_id": str(company.get("id", "")),
                "created_at": t.get("_info", {}).get("dateEntered", ""),
                "raw": t,
            })
        log.info("connectwise_psa.get_tickets", count=len(results))
        return results
