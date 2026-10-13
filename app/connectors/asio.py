"""Asio (ConnectWise Asio) connector.

Official docs: https://developer.connectwise.com → Asio Partners → Authentication
Authentication: POST /v1/token with JSON body (NOT form-encoded).
  NOTE: auth endpoint does NOT use the /api prefix.
Base URLs:
  NA: https://openapi.service.itsupport247.net
  EU: https://openapi.service.euplatform.connectwise.com
  AU: https://openapi.service.auplatform.connectwise.com
API paths (all resource endpoints use /api prefix per OpenAPI 3.0.3 spec v2.3.5):
  Companies: GET  /api/platform/v1/company/companies
  Devices:   POST /api/platform/v2/device/categories/{category}/endpoints
  Tickets:   GET  /api/platform/v2/service/ticketing/tickets
  Sites:     GET  /api/platform/v1/company/companies/{companyId}/sites
Pagination:
  Companies: cursor-based via Link header, limit param.
  Devices:   cursor-based via Link header, POST with body {resourceType, resources}.
  Tickets:   pageSize / pageNum query parameters.
Rate limiting: 10 requests / 10 minutes — retry on HTTP 429 with tenacity.
Token reuse: tokens must be reused until expiry; requesting too often → 423 Locked.
"""

import asyncio
import re
import time
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

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

# --- Production base URLs per official Asio documentation ---
_REGION_BASE_URLS: dict[str, str] = {
    "NA": "https://openapi.service.itsupport247.net",
    "EU": "https://openapi.service.euplatform.connectwise.com",
    "AU": "https://openapi.service.auplatform.connectwise.com",
}

# Token validity buffer — re-authenticate 60 s before actual expiry
_TOKEN_EXPIRY_BUFFER_S = 60

# TTL for cached API responses (seconds).  Asio allows only 10 requests
# per 10 minutes, so we cache aggressively to avoid 429 errors.
_CACHE_TTL_S = 300  # 5 minutes


def _is_rate_limited(exc: BaseException) -> bool:
    """Return True when the exception is an HTTP 429 response."""
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


def _extract_next_cursor(link_header: str) -> str | None:
    """Parse RFC 5988 Link header and return the cursor for rel=next, if present."""
    match = re.search(r'cursor=([^&>]+)[^>]*>;\s*rel="next"', link_header)
    if match:
        return match.group(1)
    return None


class AsioConnector(AbstractPlatformConnector):
    """Connector for ConnectWise Asio."""

    platform_name = "asio"
    supports_alerts = False
    supports_tickets = True
    supports_install = True
    supports_company_create = True

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialise with injected or default settings."""
        self._settings = settings or get_settings()
        self._access_token: str = ""
        self._token_expires_at: float = 0.0  # epoch seconds

        # --- Response cache (prevents concurrent 429 exhaustion) ---
        self._companies_cache: list[dict[str, Any]] = []
        self._companies_cache_ts: float = 0.0
        self._devices_cache: list["DeviceRecord"] = []
        self._devices_cache_ts: float = 0.0
        self._assets_cache: list[dict[str, Any]] = []
        self._assets_cache_ts: float = 0.0
        self._api_lock = asyncio.Lock()

    # ---- Configuration helpers ----

    def check_config(self) -> list[str]:
        missing = []
        if not self._settings.asio_client_id:
            missing.append("ASIO_CLIENT_ID")
        if not self._settings.asio_client_secret:
            missing.append("ASIO_CLIENT_SECRET")
        return missing

    def _base_url(self) -> str:
        """Return the API base URL, preferring explicit config over region lookup."""
        if self._settings.asio_base_url:
            return self._settings.asio_base_url.rstrip("/")
        region = self._settings.asio_region.upper()
        return _REGION_BASE_URLS.get(region, _REGION_BASE_URLS["NA"])

    def _token_url(self) -> str:
        """Return the OAuth token URL.  /v1/token appended to base."""
        if self._settings.asio_token_url:
            return self._settings.asio_token_url
        return f"{self._base_url()}/v1/token"

    def _api_base(self) -> str:
        """Return the API resource base URL (all resource paths use /api prefix)."""
        return f"{self._base_url()}/api"

    def get_diagnostics(self) -> dict[str, str]:
        """Return a diagnostic dict showing resolved config (for debugging)."""
        return {
            "base_url": self._base_url(),
            "api_base_url": self._api_base(),
            "token_url": self._token_url(),
            "region": self._settings.asio_region,
            "client_id_set": bool(self._settings.asio_client_id),
            "client_secret_set": bool(self._settings.asio_client_secret),
            "scope": self._settings.asio_scope,
            "granted_scope": getattr(self, "_granted_scope", "") or "(no token yet)",
            "dropped_scopes": ", ".join(getattr(self, "_dropped_scopes", ())) or "(none)",
            "has_token": bool(self._access_token),
            "token_valid": time.time() < self._token_expires_at,
            "explicit_base_url": self._settings.asio_base_url or "(not set — using region default)",
            "explicit_token_url": self._settings.asio_token_url or "(not set — derived from base)",
            "companies_url": f"{self._api_base()}/platform/v1/company/companies",
            "devices_url": f"{self._api_base()}/platform/v2/device/categories/all/endpoints (POST)",
            "tickets_url": f"{self._api_base()}/platform/v2/service/ticketing/tickets",
        }

    # ---- Authentication (with token caching) ----

    def _token_is_valid(self) -> bool:
        """Return True if the cached token hasn't expired yet."""
        return bool(self._access_token) and time.time() < self._token_expires_at

    async def authenticate(self) -> None:
        """Obtain or reuse OAuth 2.0 token using JSON body per Asio API docs.

        NOTE (from Asio docs): "The authentication token is designed to be used
        until it expires.  If the token is requested too often, before expiration,
        then you will get an error response: 423 Locked."
        """
        # Reuse cached token if still valid
        if self._token_is_valid():
            log.debug("asio.auth.cached", expires_in=int(self._token_expires_at - time.time()))
            return

        token_url = self._token_url()
        dropped = set(getattr(self, "_dropped_scopes", ()))
        scope = " ".join(s for s in self._settings.asio_scope.split() if s not in dropped)
        log.info(
            "asio.auth.start",
            token_url=token_url,
            scope=scope,
            client_id_prefix=self._settings.asio_client_id[:8] + "..." if self._settings.asio_client_id else "(empty)",
        )

        payload = {
            "grant_type": "client_credentials",
            "client_id": self._settings.asio_client_id,
            "client_secret": self._settings.asio_client_secret,
            "scope": scope,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(
                    token_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            except httpx.ConnectError as exc:
                log.error("asio.auth.connect_error", token_url=token_url, error=str(exc))
                raise RuntimeError(
                    f"Cannot connect to Asio token endpoint: {token_url} — {exc}"
                ) from exc
            except httpx.TimeoutException as exc:
                log.error("asio.auth.timeout", token_url=token_url, error=str(exc))
                raise RuntimeError(
                    f"Timeout connecting to Asio token endpoint: {token_url}"
                ) from exc

            status = response.status_code
            body_text = response.text[:1000] if response.text else "(empty body)"

            # Asio refuses the whole token if any scope is unknown — drop those and retry once, so one bad
            # scope name can't take every Asio read down.
            bad = re.search(r"Unknown/invalid scope\(s\):\s*\[([^\]]*)\]", body_text) if status == 400 else None
            if bad:
                unknown = {s.strip() for s in bad.group(1).split(",") if s.strip()}
                kept = " ".join(s for s in scope.split() if s not in unknown)
                log.warning("asio.auth.scopes_dropped", dropped=sorted(unknown), hint="remove them from ASIO_SCOPE")
                self._dropped_scopes = sorted(unknown)
                payload["scope"] = kept
                response = await client.post(token_url, json=payload, headers={"Content-Type": "application/json"})
                status = response.status_code
                body_text = response.text[:1000] if response.text else "(empty body)"

            if status == 423:
                log.error(
                    "asio.auth.locked",
                    token_url=token_url,
                    detail="Token requested too frequently — reuse until expiry",
                    body=body_text,
                )
                raise RuntimeError(
                    f"Asio 423 Locked: token was requested too often. "
                    f"Wait for current token to expire before retrying. URL: {token_url}"
                )

            if status != 200:
                log.error(
                    "asio.auth.failed",
                    token_url=token_url,
                    status=status,
                    body=body_text,
                    hint=(
                        "Check: 1) client_id/secret are correct, "
                        "2) token URL matches your Asio region, "
                        "3) scopes are valid"
                    ),
                )
                raise RuntimeError(
                    f"Asio auth failed — HTTP {status} from {token_url}: {body_text[:200]}"
                )

            data: dict[str, Any] = response.json()
            self._access_token = data.get("access_token", "")
            if not self._access_token:
                log.error("asio.auth.no_token", response_keys=list(data.keys()), body=body_text[:300])
                raise RuntimeError(
                    f"Asio auth response missing 'access_token'. Keys: {list(data.keys())}"
                )

            # Cache token with expiry (default 3600s if not provided)
            expires_in = int(data.get("expires_in", 3600))
            self._token_expires_at = time.time() + expires_in - _TOKEN_EXPIRY_BUFFER_S
            self._granted_scope = str(data.get("scope", ""))
            log.info(
                "asio.auth.success",
                expires_in=expires_in,
                granted_scope=self._granted_scope,
                token_prefix=self._access_token[:12] + "...",
            )

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    # ---- HTTP helpers ----

    @retry(
        retry=retry_if_exception(_is_rate_limited),
        wait=wait_exponential(multiplier=10, min=10, max=120),
        stop=stop_after_attempt(3),
    )
    async def _get_page(
        self, client: httpx.AsyncClient, url: str, params: dict[str, Any]
    ) -> httpx.Response:
        """Perform a GET request with retry on HTTP 429."""
        response = await client.get(url, headers=self._auth_headers(), params=params)
        if response.status_code != 200:
            body_preview = response.text[:500] if response.text else "(empty)"
            log.error(
                "asio.api.error",
                url=url,
                status=response.status_code,
                body=body_preview,
            )
        response.raise_for_status()
        return response

    @retry(
        retry=retry_if_exception(_is_rate_limited),
        wait=wait_exponential(multiplier=10, min=10, max=120),
        stop=stop_after_attempt(3),
    )
    async def _post_page(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict[str, Any],
        json_body: dict[str, Any],
    ) -> httpx.Response:
        """Perform a POST request with JSON body and retry on HTTP 429."""
        response = await client.post(
            url, headers=self._auth_headers(), params=params, json=json_body
        )
        if response.status_code != 200:
            body_preview = response.text[:500] if response.text else "(empty)"
            log.error(
                "asio.api.error",
                url=url,
                status=response.status_code,
                body=body_preview,
            )
        response.raise_for_status()
        return response

    async def _paginated_get(self, path: str) -> list[dict[str, Any]]:
        """Fetch all pages using cursor-based Link header pagination."""
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        base = f"{self._api_base()}{path}"
        page_count = 0
        log.info("asio.api.request_start", url=base)

        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                params: dict[str, Any] = {"limit": 100}
                if cursor:
                    params["cursor"] = cursor
                response = await self._get_page(client, base, params)
                batch: list[dict[str, Any]] = response.json()
                page_count += 1
                results.extend(batch)
                log.debug(
                    "asio.api.page",
                    url=base,
                    page=page_count,
                    batch_size=len(batch),
                    total_so_far=len(results),
                )
                link_header = response.headers.get("Link", "")
                cursor = _extract_next_cursor(link_header) if link_header else None
                if not cursor:
                    break

        log.info("asio.api.request_done", url=base, total=len(results), pages=page_count)
        return results

    # ---- Required connector methods ----

    async def _ensure_auth(self) -> None:
        """Authenticate if no valid token is cached."""
        if not self._token_is_valid():
            await self.authenticate()

    async def _get_companies_raw(self) -> list[dict[str, Any]]:
        """Return raw company dicts, using a TTL cache + lock to stay within rate limits.

        Multiple callers (get_companies, get_devices, get_agent_installer_info)
        all need the companies list.  Without caching each call burns a
        rate-limited request (Asio allows only 10 req / 10 min).  This helper
        ensures only ONE live request is made within the TTL window; all
        concurrent waiters share the same cached result.
        """
        now = time.time()
        if self._companies_cache and (now - self._companies_cache_ts) < _CACHE_TTL_S:
            log.debug(
                "asio.companies_cache.hit",
                age_s=int(now - self._companies_cache_ts),
                count=len(self._companies_cache),
            )
            return self._companies_cache

        async with self._api_lock:
            # Double-check after acquiring lock (another coroutine may have filled it)
            now = time.time()
            if self._companies_cache and (now - self._companies_cache_ts) < _CACHE_TTL_S:
                return self._companies_cache

            await self._ensure_auth()
            data = await self._paginated_get("/platform/v1/company/companies")
            self._companies_cache = data
            self._companies_cache_ts = time.time()
            log.info("asio.companies_cache.refreshed", count=len(data))
            return data

    async def _get_assets_raw(self) -> list[dict[str, Any]]:
        """Return raw Asio assets, best-effort and cached.

        The normal endpoint inventory API does not consistently include a
        last-online/check-in timestamp.  Asio's asset endpoint can expose extra
        lifecycle/status fields depending on tenant scopes, so we merge matching
        asset records into device ``raw`` payloads for downstream extraction.
        """
        now = time.time()
        if self._assets_cache and (now - self._assets_cache_ts) < _CACHE_TTL_S:
            log.debug(
                "asio.assets_cache.hit",
                age_s=int(now - self._assets_cache_ts),
                count=len(self._assets_cache),
            )
            return self._assets_cache

        async with self._api_lock:
            now = time.time()
            if self._assets_cache and (now - self._assets_cache_ts) < _CACHE_TTL_S:
                return self._assets_cache

            await self._ensure_auth()
            try:
                data = await self._paginated_get("/platform/v1/asset/assets")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (403, 404):
                    log.warning(
                        "asio.assets.not_available",
                        status=exc.response.status_code,
                        detail="Asset endpoint unavailable or missing platform.asset.read scope",
                    )
                    self._assets_cache = []
                    self._assets_cache_ts = time.time()
                    return []
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("asio.assets.error", error=str(exc))
                self._assets_cache = []
                self._assets_cache_ts = time.time()
                return []

            self._assets_cache = data
            self._assets_cache_ts = time.time()
            log.info("asio.assets_cache.refreshed", count=len(data))
            return data

    @staticmethod
    def _first_nested_value(source: dict[str, Any], paths: tuple[tuple[str, ...], ...]) -> Any:
        """Return the first non-empty value from a list of nested key paths."""
        for path in paths:
            current: Any = source
            for key in path:
                if not isinstance(current, dict) or key not in current:
                    current = None
                    break
                current = current[key]
            if current not in (None, "", []):
                return current
        return ""

    def _build_asset_lookups(
        self,
        assets: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Build endpoint, hostname, and serial lookup maps for asset payloads."""
        by_endpoint_id: dict[str, dict[str, Any]] = {}
        by_hostname: dict[str, dict[str, Any]] = {}
        by_serial: dict[str, dict[str, Any]] = {}

        endpoint_paths = (
            ("endpointID",), ("endpointId",), ("endpoint_id",),
            ("endpoint", "endpointID"), ("endpoint", "endpointId"),
            ("device", "endpointID"), ("device", "endpointId"),
        )
        hostname_paths = (
            ("deviceName",), ("hostName",), ("hostname",), ("computerName",),
            ("name",), ("assetName",), ("endpoint", "deviceName"),
            ("device", "name"), ("device", "hostName"),
        )
        serial_paths = (
            ("serialNumber",), ("serial_number",), ("system", "serialNumber"),
            ("hardware", "serialNumber"), ("device", "serialNumber"),
        )

        for asset in assets:
            if not isinstance(asset, dict):
                continue
            endpoint_id = str(self._first_nested_value(asset, endpoint_paths) or "").strip().lower()
            if endpoint_id:
                by_endpoint_id[endpoint_id] = asset

            hostname = normalize_hostname(str(self._first_nested_value(asset, hostname_paths) or ""))
            if hostname:
                by_hostname[hostname] = asset

            serial = normalize_serial(str(self._first_nested_value(asset, serial_paths) or ""))
            if serial:
                by_serial[serial] = asset

        return by_endpoint_id, by_hostname, by_serial

    async def get_companies(self) -> list[CompanyRecord]:
        """Return all Asio companies (cached)."""
        data = await self._get_companies_raw()
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
        log.info("asio.get_companies", count=len(records))
        return records

    async def get_devices(self) -> list[DeviceRecord]:
        """Return all Asio devices using POST /api/platform/v2/device/categories/all/endpoints.

        The v2 devices endpoint requires a POST with a JSON body specifying
        resourceType and resources (company IDs).  Pagination is cursor-based
        via the Link header (max 500 per page).

        Each response group contains ``companyID`` and ``siteID``.  We use the
        company name as site_name fallback (the per-company sites endpoint is
        too rate-limited for bulk fetching).

        Results are cached for ``_CACHE_TTL_S`` seconds to avoid burning
        rate-limited requests on repeated calls.
        """
        # Return cached devices if fresh
        now = time.time()
        if self._devices_cache and (now - self._devices_cache_ts) < _CACHE_TTL_S:
            log.debug(
                "asio.devices_cache.hit",
                age_s=int(now - self._devices_cache_ts),
                count=len(self._devices_cache),
            )
            return list(self._devices_cache)

        # Fetch companies/assets BEFORE acquiring _api_lock to avoid deadlock:
        # _get_companies_raw() also uses _api_lock (asyncio.Lock is not reentrant).
        companies = await self._get_companies_raw()
        assets = await self._get_assets_raw()
        assets_by_endpoint_id, assets_by_hostname, assets_by_serial = self._build_asset_lookups(assets)

        async with self._api_lock:
            # Double-check after acquiring lock
            now = time.time()
            if self._devices_cache and (now - self._devices_cache_ts) < _CACHE_TTL_S:
                return list(self._devices_cache)

            await self._ensure_auth()

            company_ids = [str(c.get("id", "")) for c in companies if c.get("id")]

            if not company_ids:
                log.warning("asio.get_devices.no_companies", detail="No companies found, cannot fetch devices")
                return []

            # Build a company-id → name lookup for enrichment
            company_name_map: dict[str, str] = {
                str(c.get("id", "")): c.get("name", "") for c in companies
            }

            records: list[DeviceRecord] = []
            url = f"{self._api_base()}/platform/v2/device/categories/all/endpoints"

            # Process companies in batches of 4 (API limit per request)
            batch_size = 4
            for i in range(0, len(company_ids), batch_size):
                batch_ids = company_ids[i : i + batch_size]
                json_body: dict[str, Any] = {
                    "resourceType": "company",
                    "resources": batch_ids,
                }
                cursor: int = 0
                page_count = 0

                async with httpx.AsyncClient(timeout=30.0) as client:
                    while True:
                        params: dict[str, Any] = {"limit": 500}
                        if cursor:
                            params["cursor"] = cursor
                        response = await self._post_page(client, url, params, json_body)
                        data: dict[str, Any] = response.json()
                        page_count += 1

                        # Only include "platform" category (workstations/servers).
                        # Skip "network" (switches, APs, firewalls) and "cloud" devices.
                        for group in data.get("platform", []):
                            company_id = str(group.get("companyID", ""))
                            site_id = str(group.get("siteID", ""))
                            company_name = company_name_map.get(company_id, "")
                            for ep in group.get("endpoints", []):
                                hostname = ep.get("deviceName", "") or ep.get("friendlyName", "")
                                ep_id = str(ep.get("endpointID", ""))
                                serial_number = normalize_serial(
                                    ep.get("system", {}).get("serialNumber", "")
                                    if isinstance(ep.get("system"), dict)
                                    else ""
                                )
                                mac_list = [normalize_mac(m) for m in (ep.get("macAddress") or [])]
                                hostname_norm = normalize_hostname(hostname)
                                asset = (
                                    assets_by_endpoint_id.get(ep_id.lower())
                                    or assets_by_hostname.get(hostname_norm)
                                    or assets_by_serial.get(serial_number)
                                )
                                raw = dict(ep)
                                if asset:
                                    raw["asset"] = asset
                                records.append(
                                    DeviceRecord(
                                        platform=self.platform_name,
                                        platform_id=ep_id,
                                        hostname=hostname_norm,
                                        serial_number=serial_number,
                                        mac_addresses=[m for m in mac_list if m],
                                        company_platform_id=company_id,
                                        company_name=company_name,
                                        site_id=site_id,
                                        site_name=company_name,
                                        endpoint_id=ep_id,
                                        raw=raw,
                                    )
                                )

                        link_header = response.headers.get("Link", "")
                        next_cursor = _extract_next_cursor(link_header) if link_header else None
                        if not next_cursor:
                            break
                        cursor = int(next_cursor)

                log.debug(
                    "asio.get_devices.batch",
                    batch_start=i,
                    batch_ids=batch_ids,
                    pages=page_count,
                    records_so_far=len(records),
                )

            # Store in cache
            self._devices_cache = records
            self._devices_cache_ts = time.time()
            log.info("asio.get_devices", count=len(records))
            return list(records)

    # ---- Antivirus evidence + endpoint custom fields (AV gap review) ----

    _AV_FIELDS = ("endpointID,deviceName,friendlyName,endpointType,antiViruses,modifiedAt,agentInstalledUTC,"
                  "rolesFeatures,os")

    async def get_endpoints_antivirus(self, endpoint_ids: list[str]) -> list[dict[str, Any]]:
        """Antivirus products Asio detects on each endpoint.

        POST /api/platform/v2/device/categories/all/endpoints with resourceType=endpoint (500 ids per call, so a
        full sweep is a handful of requests) and a small field projection. Returns one dict per endpoint:
        {endpoint_id, company_id, site_id, hostname, antiviruses: [{name, version, applicationName}],
        modified_at, agent_installed_at}.
        """
        await self._ensure_auth()
        url = f"{self._api_base()}/platform/v2/device/categories/all/endpoints"
        out: list[dict[str, Any]] = []
        ids = [str(i) for i in endpoint_ids if i]
        async with self._api_lock:
            async with httpx.AsyncClient(timeout=60.0) as client:
                for i in range(0, len(ids), 500):
                    body = {"resourceType": "endpoint", "resources": ids[i: i + 500]}
                    cursor = 0
                    while True:
                        params: dict[str, Any] = {"limit": 500, "field": self._AV_FIELDS}
                        if cursor:
                            params["cursor"] = cursor
                        response = await self._post_page(client, url, params, body)
                        data: dict[str, Any] = response.json()
                        for category in ("platform", "network", "cloud"):
                            for group in data.get(category, []) or []:
                                for ep in group.get("endpoints", []) or []:
                                    out.append({
                                        "endpoint_id": str(ep.get("endpointID", "")),
                                        "company_id": str(group.get("companyID", "")),
                                        "site_id": str(group.get("siteID", "")),
                                        "hostname": ep.get("deviceName") or ep.get("friendlyName") or "",
                                        "endpoint_type": ep.get("endpointType", ""),
                                        "antiviruses": [a for a in (ep.get("antiViruses") or []) if isinstance(a, dict)],
                                        "modified_at": ep.get("modifiedAt") or "",
                                        "agent_installed_at": ep.get("agentInstalledUTC") or "",
                                        "roles": [str(r) for r in (ep.get("rolesFeatures") or []) if r],
                                        "os_product": ((ep.get("os") or {}).get("product") or "")
                                        if isinstance(ep.get("os"), dict) else "",
                                    })
                        nxt = _extract_next_cursor(response.headers.get("Link", "")) if response.headers.get("Link") else None
                        if not nxt:
                            break
                        cursor = int(nxt)
        log.info("asio.endpoints_antivirus", requested=len(ids), returned=len(out))
        return out

    async def get_custom_field_definitions(self) -> list[dict[str, Any]]:
        """GET /api/platform/v1/custom-field/definitions → [{id, name, entityType…}] (empty list if unavailable)."""
        await self._ensure_auth()
        url = f"{self._api_base()}/platform/v1/custom-field/definitions"
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=self._auth_headers(), params={"limit": 200})
        if r.status_code != 200:
            log.warning("asio.custom_field_definitions.failed", status=r.status_code, body=r.text[:300])
            return []
        data = r.json()
        rows = data if isinstance(data, list) else (data.get("definitions") or data.get("data") or data.get("items") or [])
        return [d for d in rows if isinstance(d, dict)]

    async def set_endpoint_custom_field(self, endpoint_id: str, attribute_id: str, value: str) -> dict[str, Any]:
        """PUT /api/platform/v2/device/endpoints/{id}/custom-fields → {ok, status, body}."""
        await self._ensure_auth()
        url = f"{self._api_base()}/platform/v2/device/endpoints/{endpoint_id}/custom-fields"
        body = [{"entityId": endpoint_id, "attributeId": attribute_id, "value": value}]
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.put(url, headers={**self._auth_headers(), "Content-Type": "application/json"}, json=body)
            if r.status_code == 403:  # permissions are baked into the token — retry once with a fresh one
                self._access_token, self._token_expires_at = "", 0
                await self._ensure_auth()
                r = await client.put(url, headers={**self._auth_headers(), "Content-Type": "application/json"},
                                     json=body)
        try:
            parsed: Any = r.json() if r.content else None
        except ValueError:
            parsed = r.text[:500]
        return {"ok": 200 <= r.status_code < 300, "status": r.status_code, "body": parsed}

    # ---- Optional capability methods ----

    async def get_alerts(
        self, *, limit: int = 50, severity: str | None = None
    ) -> list[dict[str, Any]] | None:
        """Asio has no security threats/alerts endpoint in the OpenAPI spec.

        Returns ``None`` to indicate the capability is unsupported.
        """
        return None

    async def get_tickets(
        self, *, company_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]] | None:
        """Return tickets from Asio using GET /api/platform/v2/service/ticketing/tickets.

        Uses pageSize / pageNum pagination per the OpenAPI spec.
        """
        await self._ensure_auth()
        try:
            url = f"{self._api_base()}/platform/v2/service/ticketing/tickets"
            tickets: list[dict[str, Any]] = []
            page_num = 1
            page_size = min(limit, 100)

            async with httpx.AsyncClient(timeout=30.0) as client:
                while len(tickets) < limit:
                    params: dict[str, Any] = {
                        "pageSize": page_size,
                        "pageNum": page_num,
                    }
                    if company_id:
                        params["companyIds"] = company_id
                    response = await self._get_page(client, url, params)
                    data: dict[str, Any] = response.json()

                    # v2 returns {tickets: [...], totalCount: N}
                    batch = data.get("tickets", [])
                    if not batch:
                        break

                    for item in batch:
                        company_obj = item.get("company") or {}
                        priority_obj = item.get("priority") or {}
                        status_obj = item.get("status") or {}
                        tickets.append({
                            "platform": self.platform_name,
                            "ticket_id": item.get("number", str(item.get("id", ""))),
                            "summary": item.get("summary", ""),
                            "company_name": company_obj.get("name", "") if isinstance(company_obj, dict) else "",
                            "priority": priority_obj.get("name", "") if isinstance(priority_obj, dict) else str(priority_obj),
                            "status": status_obj.get("name", "") if isinstance(status_obj, dict) else str(status_obj),
                            "created_at": item.get("createdAt", ""),
                        })
                        if len(tickets) >= limit:
                            break

                    total_count = data.get("totalCount", 0)
                    if page_num * page_size >= total_count:
                        break
                    page_num += 1

            log.info("asio.get_tickets", count=len(tickets))
            return tickets
        except Exception as exc:  # noqa: BLE001
            log.warning("asio.get_tickets.error", error=str(exc))
            return []

    # ---- Device group / custom field helpers ─────────────────────

    async def get_device_groups(self) -> list[dict[str, Any]]:
        """Return all device groups from ``GET /api/platform/v1/device/groups``.

        Requires the ``platform.deviceGroups.read`` scope which must be added
        to the OAuth client configuration in the ConnectWise developer portal.
        Returns an empty list if the endpoint is unavailable (404/403).
        """
        await self._ensure_auth()
        url = f"{self._api_base()}/platform/v1/device/groups"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=self._auth_headers())
                if response.status_code == 404:
                    log.warning(
                        "asio.device_groups.not_available",
                        detail="Endpoint 404 — add platform.deviceGroups.read scope",
                    )
                    return []
                response.raise_for_status()
                data = response.json()
                groups = data if isinstance(data, list) else data.get("groups", [])
                log.info("asio.device_groups", count=len(groups))
                return groups
        except Exception as exc:  # noqa: BLE001
            log.warning("asio.device_groups.error", error=str(exc))
            return []

    async def get_devices_in_group(self, group_id: str) -> list[dict[str, Any]]:
        """Return endpoints belonging to a specific device group.

        Requires ``platform.deviceGroups.read`` scope.  Handles Link-header
        pagination so groups with >500 members are fully retrieved.
        """
        await self._ensure_auth()
        url = f"{self._api_base()}/platform/v1/device/groups/{group_id}/endpoints"
        all_endpoints: list[dict[str, Any]] = []

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                cursor: int = 0
                while True:
                    params: dict[str, Any] = {"limit": 500}
                    if cursor:
                        params["cursor"] = cursor
                    response = await client.get(
                        url, headers=self._auth_headers(), params=params,
                    )
                    if response.status_code == 404:
                        log.warning("asio.devices_in_group.not_found", group_id=group_id)
                        return []
                    response.raise_for_status()
                    data = response.json()
                    endpoints = data if isinstance(data, list) else data.get("endpoints", [])
                    all_endpoints.extend(endpoints)

                    link_header = response.headers.get("Link", "")
                    next_cursor = _extract_next_cursor(link_header) if link_header else None
                    if not next_cursor:
                        break
                    cursor = int(next_cursor)

        except Exception as exc:  # noqa: BLE001
            log.warning("asio.devices_in_group.error", group_id=group_id, error=str(exc))
            return all_endpoints

        log.info("asio.devices_in_group", group_id=group_id, count=len(all_endpoints))
        return all_endpoints

    async def get_endpoint_applications(
        self,
        endpoint_ids: list[str],
        *,
        app_name_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch installed applications for a list of endpoint IDs.

        Uses ``POST /api/platform/v2/device/endpoints/applications``.

        Args:
            endpoint_ids: ASIO endpoint IDs to query (batched in groups of 500).
            app_name_filter: Optional application name filter (e.g. ``"ESET"``).
                Passed as the ``name`` query parameter.

        Returns a list of dicts, each containing::

            {
                "companyID": "...",
                "siteID": "...",
                "endpointID": "...",
                "applications": [
                    {"name": "...", "version": "...", "publisher": "...",
                     "lastAccessDateTime": "...", ...}
                ]
            }

        Only entries with at least one application are included.
        """
        if not endpoint_ids:
            return []

        await self._ensure_auth()
        url = f"{self._api_base()}/platform/v2/device/endpoints/applications"
        all_results: list[dict[str, Any]] = []

        # Endpoint IDs can be sent in batches of up to 500
        batch_size = 500
        for i in range(0, len(endpoint_ids), batch_size):
            batch_ids = endpoint_ids[i : i + batch_size]
            json_body: dict[str, Any] = {
                "resourceType": "endpoint",
                "resources": batch_ids,
            }
            cursor: int = 0

            async with httpx.AsyncClient(timeout=60.0) as client:
                while True:
                    params: dict[str, Any] = {"limit": 500}
                    if cursor:
                        params["cursor"] = cursor
                    if app_name_filter:
                        params["name"] = app_name_filter

                    try:
                        response = await self._post_page(client, url, params, json_body)
                    except httpx.HTTPStatusError as exc:
                        log.warning(
                            "asio.endpoint_applications.error",
                            status=exc.response.status_code,
                            batch_start=i,
                            detail=exc.response.text[:300],
                        )
                        break

                    data = response.json()
                    # Response is a list of endpoint groups
                    batch_results = data if isinstance(data, list) else []
                    for entry in batch_results:
                        apps = entry.get("applications", [])
                        if apps:
                            all_results.append(entry)

                    link_header = response.headers.get("Link", "")
                    next_cursor = _extract_next_cursor(link_header) if link_header else None
                    if not next_cursor:
                        break
                    cursor = int(next_cursor)

            log.debug(
                "asio.endpoint_applications.batch",
                batch_start=i,
                batch_count=len(batch_ids),
                results_so_far=len(all_results),
            )

        log.info(
            "asio.endpoint_applications.done",
            total_endpoints_queried=len(endpoint_ids),
            endpoints_with_apps=len(all_results),
            app_filter=app_name_filter,
        )
        return all_results

    async def get_custom_field_definitions(self) -> list[dict[str, Any]]:
        """Return custom field definitions from ASIO.

        Requires ``platform.custom_fields_definitions.read`` scope.
        """
        await self._ensure_auth()
        url = f"{self._api_base()}/platform/v1/custom-fields/definitions"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=self._auth_headers())
                if response.status_code == 404:
                    log.warning(
                        "asio.custom_fields.not_available",
                        detail="Endpoint 404 — add custom_fields scopes",
                    )
                    return []
                response.raise_for_status()
                data = response.json()
                defs = data if isinstance(data, list) else data.get("definitions", [])
                log.info("asio.custom_field_definitions", count=len(defs))
                return defs
        except Exception as exc:  # noqa: BLE001
            log.warning("asio.custom_field_definitions.error", error=str(exc))
            return []

    # ---- Site helpers ────────────────────────────────────────────

    async def get_sites(self, company_id: str) -> list[dict[str, Any]]:
        """Return all sites for a given company.

        Uses GET /api/platform/v1/company/companies/{companyId}/sites.
        Each site typically includes an ``agentSetupToken`` (or similar)
        field used to construct the barebone agent installer URL.
        """
        await self._ensure_auth()
        url = f"{self._api_base()}/platform/v1/company/companies/{company_id}/sites"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await self._get_page(client, url, {})
                data = response.json()
                sites = data if isinstance(data, list) else data.get("sites", [])
                log.info("asio.get_sites", company_id=company_id, count=len(sites))
                return sites
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 403):
                log.warning(
                    "asio.get_sites.not_available",
                    company_id=company_id,
                    status=exc.response.status_code,
                )
                return []
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("asio.get_sites.error", company_id=company_id, error=str(exc))
            return []

    async def get_agent_installer_info(
        self, company_id: str, site_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the agent installer URL and token for a company/site.

        Fetches sites for the company, picks the matching site (or first
        if *site_id* is ``None``), and constructs the MSI download URL
        using the Asio barebone agent pattern::

            https://prod.setup.itsupport247.net/windows/BareboneAgent/32/
            {Company}_-_{Site}-{Company}_{name}_Windows_OS_ITSPlatform_TKN{uuid}/MSI/setup

        The response includes ``download_url``, ``msi_filename``, ``token``,
        ``company_name``, and ``site_name``.
        """
        sites = await self.get_sites(company_id)
        if not sites:
            return {"error": f"No sites found for company {company_id}"}

        # Find the requested site or default to first
        site: dict[str, Any] | None = None
        if site_id:
            site = next(
                (s for s in sites if str(s.get("id", "")) == site_id),
                None,
            )
            if site is None:
                return {
                    "error": f"Site {site_id} not found for company {company_id}",
                    "available_sites": [
                        {"id": str(s.get("id", "")), "name": s.get("name", "")}
                        for s in sites
                    ],
                }
        else:
            site = sites[0]

        site_name = site.get("name", "")

        # Look for an agent token in common field names
        token = (
            site.get("agentSetupToken", "")
            or site.get("setupToken", "")
            or site.get("agentToken", "")
            or site.get("installToken", "")
            or site.get("token", "")
        )

        # Also look for a pre-built installer URL
        installer_url = (
            site.get("agentInstallerUrl", "")
            or site.get("installerUrl", "")
            or site.get("setupUrl", "")
        )

        # Fetch the company name for building the URL path (cached)
        companies = await self._get_companies_raw()
        company_name = ""
        for c in companies:
            if str(c.get("id", "")) == str(company_id):
                company_name = c.get("name", "")
                break

        log.info(
            "asio.agent_installer_info",
            company_id=company_id,
            company_name=company_name,
            site_id=str(site.get("id", "")),
            site_name=site_name,
            has_token=bool(token),
            has_installer_url=bool(installer_url),
            site_keys=list(site.keys()),
        )

        return {
            "company_id": company_id,
            "company_name": company_name,
            "site_id": str(site.get("id", "")),
            "site_name": site_name,
            "token": token,
            "installer_url": installer_url,
            "site_raw": site,
            "all_sites": [
                {"id": str(s.get("id", "")), "name": s.get("name", "")}
                for s in sites
            ],
        }

    # ---- Remediation capabilities ────────────────────────────────

    async def list_scripts(
        self, *, company_id: str | None = None
    ) -> list[dict[str, Any]] | None:
        """List available automation scripts from Asio.

        Uses GET /api/platform/v1/automation/scripts.
        Requires ``platform.automation.read`` scope.
        """
        await self._ensure_auth()
        url = f"{self._api_base()}/platform/v1/automation/scripts"
        try:
            all_scripts: list[dict[str, Any]] = []
            async with httpx.AsyncClient(timeout=30.0) as client:
                params: dict[str, Any] = {"limit": 100}
                if company_id:
                    params["companyId"] = company_id
                response = await self._get_page(client, url, params)
                data = response.json()
                scripts = data if isinstance(data, list) else data.get("scripts", [])
                for s in scripts:
                    all_scripts.append({
                        "id": str(s.get("id", "")),
                        "name": s.get("name", ""),
                        "description": s.get("description", ""),
                        "category": s.get("category", ""),
                        "os_type": s.get("osType", s.get("operatingSystem", "")),
                        "company_id": str(s.get("companyId", s.get("companyID", ""))),
                        "enabled": s.get("enabled", True),
                    })
            log.info("asio.list_scripts", count=len(all_scripts))
            return all_scripts
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 403):
                log.warning(
                    "asio.list_scripts.not_available",
                    status=exc.response.status_code,
                    detail="Add platform.automation.read scope to OAuth client",
                )
                return []
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("asio.list_scripts.error", error=str(exc))
            return []

    async def run_script(
        self,
        script_id: str,
        device_ids: list[str],
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Execute an automation script on target Asio endpoints.

        Primary path uses POST /api/platform/v1/automation/scripts/{scriptId}/run.
        Some tenants/environments expose script run on v2 and/or expect
        ``deviceIds`` instead of ``endpointIds``. We attempt safe fallbacks
        on 404 while preserving diagnostics.
        """
        await self._ensure_auth()

        sid = str(script_id or "").strip()
        if not sid:
            return {"script_id": sid, "error": "Missing script_id"}

        parameters = kwargs.get("parameters") if isinstance(kwargs.get("parameters"), dict) else None
        attempts: list[dict[str, Any]] = []
        request_variants = [
            ("v1", "endpointIds", True),
            ("v2", "endpointIds", True),
            ("v1", "deviceIds", True),
            ("v2", "deviceIds", True),
            # Some tenants expose run endpoint without script id in path
            # and expect scriptId in JSON body.
            ("v1", "endpointIds", False),
            ("v2", "endpointIds", False),
            ("v1", "deviceIds", False),
            ("v2", "deviceIds", False),
        ]

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                for api_version, ids_field, script_in_path in request_variants:
                    if script_in_path:
                        url = f"{self._api_base()}/platform/{api_version}/automation/scripts/{sid}/run"
                    else:
                        url = f"{self._api_base()}/platform/{api_version}/automation/scripts/run"
                    json_body: dict[str, Any] = {ids_field: device_ids}
                    if not script_in_path:
                        json_body["scriptId"] = sid
                    if parameters:
                        json_body["parameters"] = parameters

                    try:
                        response = await client.post(
                            url,
                            headers=self._auth_headers(),
                            json=json_body,
                        )
                        response.raise_for_status()
                        body = response.json()
                        log.info(
                            "asio.run_script",
                            script_id=sid,
                            devices=len(device_ids),
                            api_version=api_version,
                            ids_field=ids_field,
                            script_in_path=script_in_path,
                            response_keys=list(body.keys()) if isinstance(body, dict) else "non-dict",
                        )
                        return {
                            "script_id": sid,
                            "devices": len(device_ids),
                            "http_status": response.status_code,
                            "request_id": response.headers.get("X-Request-ID") or response.headers.get("x-request-id") or "",
                            "run_api_version": api_version,
                            "run_ids_field": ids_field,
                            "run_attempts": attempts,
                            "run_script_in_path": script_in_path,
                            "result": body,
                        }
                    except httpx.HTTPStatusError as exc:
                        response_text = exc.response.text or ""
                        request_id = exc.response.headers.get("X-Request-ID") or exc.response.headers.get("x-request-id") or ""
                        attempts.append({
                            "api_version": api_version,
                            "ids_field": ids_field,
                            "script_in_path": script_in_path,
                            "http_status": exc.response.status_code,
                            "request_id": request_id,
                            "response_body": response_text[:300],
                        })
                        # 404 likely indicates path mismatch for this tenant; try fallback variants.
                        if exc.response.status_code == 404:
                            continue
                        log.error(
                            "asio.run_script.error",
                            script_id=sid,
                            status=exc.response.status_code,
                            api_version=api_version,
                            ids_field=ids_field,
                            script_in_path=script_in_path,
                            request_id=request_id,
                            body=response_text[:300],
                        )
                        return {
                            "script_id": sid,
                            "http_status": exc.response.status_code,
                            "request_id": request_id,
                            "run_api_version": api_version,
                            "run_ids_field": ids_field,
                            "run_attempts": attempts,
                            "run_script_in_path": script_in_path,
                            "response_body": response_text[:500],
                            "error": f"HTTP {exc.response.status_code}: {response_text[:200]}",
                        }

            # All variants failed (most likely path mismatch or invalid script scope/id).
            last = attempts[-1] if attempts else {}
            return {
                "script_id": sid,
                "http_status": last.get("http_status", 0),
                "request_id": last.get("request_id", ""),
                "run_attempts": attempts,
                "response_body": last.get("response_body", ""),
                "error": "HTTP 404: not found for all script run endpoint variants",
            }
        except Exception as exc:  # noqa: BLE001
            log.error("asio.run_script.exception", script_id=sid, error=str(exc))
            return {"script_id": sid, "error": str(exc)}

    async def get_endpoint_tasks_summary(
        self,
        endpoint_id: str,
    ) -> list[dict[str, Any]]:
        """Get automation task summary for an endpoint.

        Uses GET /api/platform/v2/automation/endpoints/{endpointId}/tasks/summary.
        """
        await self._ensure_auth()
        if not endpoint_id:
            return []

        url = f"{self._api_base()}/platform/v2/automation/endpoints/{endpoint_id}/tasks/summary"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=self._auth_headers())
                response.raise_for_status()
                body = response.json()
                if isinstance(body, list):
                    return [t for t in body if isinstance(t, dict)]
                if isinstance(body, dict) and isinstance(body.get("results"), list):
                    return [t for t in body["results"] if isinstance(t, dict)]
                return []
        except httpx.HTTPStatusError as exc:
            log.warning(
                "asio.get_endpoint_tasks_summary.error",
                endpoint_id=endpoint_id,
                status=exc.response.status_code,
                body=exc.response.text[:300],
            )
            return []
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "asio.get_endpoint_tasks_summary.exception",
                endpoint_id=endpoint_id,
                error=str(exc),
            )
            return []

    async def install_agent(
        self, device_ids: list[str], *, company_id: str = "", **kwargs: Any
    ) -> dict[str, Any] | None:
        """Prepare ASIO agent installation info.

        ASIO agent installation is always executed **via ScreenConnect**.
        This method fetches the company/site data from ASIO to obtain the
        installer token and download URL, then returns the info needed for
        ScreenConnect to run the PowerShell install command.

        Flow:
        1. Fetch company sites from ASIO API.
        2. Extract installer token + build download URL.
        3. Return ``download_url``, ``msi_filename``, and install metadata.

        The remediation orchestrator passes this info to ScreenConnect's
        ``install_agent(agent_type="asio")`` which sends the PowerShell
        command to the target sessions.

        If ``download_url`` is provided in kwargs, skip the ASIO API lookup
        and use the provided URL directly.
        """
        download_url = kwargs.get("download_url", "")
        msi_filename = kwargs.get("msi_filename", "")
        site_id = kwargs.get("site_id", "")

        if download_url:
            # Caller already has the URL — no need to hit ASIO API
            if not msi_filename:
                # Extract filename from the URL path
                msi_filename = download_url.rstrip("/").rsplit("/", 2)[0].rsplit("/", 1)[-1] + ".msi"
            log.info(
                "asio.install_agent.direct_url",
                download_url=download_url[:80],
                devices=len(device_ids),
            )
            return {
                "download_url": download_url,
                "msi_filename": msi_filename,
                "devices": device_ids,
                "method": "screenconnect",
            }

        # Fetch installer info from ASIO company/site
        if not company_id:
            return {
                "error": "company_id is required for ASIO agent install "
                "(needed to look up site and agent token)",
            }

        info = await self.get_agent_installer_info(company_id, site_id or None)
        if info and info.get("error"):
            return info

        # Build the download URL from the installer info
        token = info.get("token", "") if info else ""
        installer_url = info.get("installer_url", "") if info else ""
        company_name = info.get("company_name", "") if info else ""
        site_name = info.get("site_name", "") if info else ""

        if installer_url:
            download_url = installer_url
        elif token:
            # Build URL from the Asio barebone agent pattern
            # Pattern: {Company}_-_{Site}-{Company}_{fullname}_Windows_OS_ITSPlatform_TKN{uuid}
            from urllib.parse import quote

            safe_company = quote(company_name.replace(" ", "_"), safe="")
            safe_site = quote(site_name.replace(" ", "_"), safe="")
            token_path = (
                f"{safe_company}_-_{safe_site}-{safe_company}"
                f"_Windows_OS_ITSPlatform_TKN{token}"
            )
            download_url = (
                f"https://prod.setup.itsupport247.net/windows/"
                f"BareboneAgent/32/{token_path}/MSI/setup"
            )
            msi_filename = f"{token_path}.msi"
        else:
            # Return the site info so the user can construct the URL manually
            return {
                "error": "Could not find agent token in site data. "
                "Provide download_url directly or check site configuration in ASIO.",
                "site_info": info,
                "hint": "Pass download_url and msi_filename from the ASIO plugin directly.",
            }

        log.info(
            "asio.install_agent.prepared",
            company=company_name,
            site=site_name,
            download_url=download_url[:80],
            devices=len(device_ids),
        )
        return {
            "download_url": download_url,
            "msi_filename": msi_filename or "asio_agent.msi",
            "company_name": company_name,
            "site_name": site_name,
            "token": token,
            "devices": device_ids,
            "method": "screenconnect",
        }

    async def create_company_on_platform(
        self, name: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Create a new company in Asio.

        Uses POST /api/platform/v1/company/companies.
        """
        await self._ensure_auth()
        url = f"{self._api_base()}/platform/v1/company/companies"

        payload: dict[str, Any] = {"name": name}
        if kwargs.get("address"):
            payload["address"] = kwargs["address"]
        if kwargs.get("phone"):
            payload["phone"] = kwargs["phone"]

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    headers=self._auth_headers(),
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
                log.info("asio.create_company", name=name, id=body.get("id"))
                return {
                    "platform": self.platform_name,
                    "id": str(body.get("id", "")),
                    "name": body.get("name", name),
                    "raw": body,
                }
        except httpx.HTTPStatusError as exc:
            log.error(
                "asio.create_company.error",
                status=exc.response.status_code,
                body=exc.response.text[:300],
            )
            return {"error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"}

    #: Asio endpoint-uninstall route. Per the ConnectWise Platform spec this is
    #: a ``DELETE`` that carries the endpoint(s) in the request body (schema
    #: ``MultipleEndpointUninstallRequest``). Requires ``platform.agent.delete``.
    _REMOVE_AGENT_PATH: str = "/platform/v2/device/endpoints/uninstall"

    async def _resolve_endpoint_ids(
        self, endpoint_id: str
    ) -> tuple[str | None, str | None]:
        """Return ``(client_id, site_id)`` for ``endpoint_id`` from cache.

        The uninstall payload is most reliable when it includes the platform
        ``clientId`` (company) and ``siteId`` alongside the ``endpointId``.
        These are already captured by :meth:`get_devices`, so look them up from
        the cached device records without forcing a fresh fetch when possible.
        """
        try:
            devices = await self.get_devices()
        except Exception:  # noqa: BLE001 - lookup is best-effort
            return None, None
        for dev in devices:
            if getattr(dev, "endpoint_id", "") == endpoint_id:
                client_id = getattr(dev, "company_platform_id", "") or None
                site_id = getattr(dev, "site_id", "") or None
                return client_id, site_id
        return None, None

    async def remove_agent(
        self, endpoint_id: str, reason: str | None = None, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Uninstall an agent/endpoint from Asio.

        Uses ``DELETE /api/platform/v2/device/endpoints/uninstall`` with the
        ``MultipleEndpointUninstallRequest`` body (``reason`` + ``endpoints``).
        The platform ``clientId``/``siteId`` are resolved from the cached device
        records so the uninstall targets the correct endpoint. Requires the
        ``platform.agent.delete`` scope ("Delete agent data").

        This is used to clean up endpoints that exist in Asio but no longer
        have a ScreenConnect session and have not communicated recently
        (e.g. past 45 days), so they are considered decommissioned.

        Parameters
        ----------
        endpoint_id : str
            The Asio endpoint ID (UUID) to uninstall.
        reason : str, optional
            Human-readable reason recorded with the uninstall request.

        Returns
        -------
        dict with ``status`` on success or ``error`` on failure.
        """
        if not endpoint_id:
            return {"error": "endpoint_id is required to remove an Asio agent"}

        await self._ensure_auth()

        client_id, site_id = await self._resolve_endpoint_ids(endpoint_id)

        endpoint_entry: dict[str, Any] = {
            "endpointId": endpoint_id,
            "uninstallType": ["RMM"],
        }
        if client_id:
            endpoint_entry["clientId"] = client_id
        if site_id:
            endpoint_entry["siteId"] = site_id

        json_body: dict[str, Any] = {
            "reason": reason or "Decommissioned (no ScreenConnect session, inactive 45+ days)",
            "endpoints": [endpoint_entry],
        }

        url = f"{self._api_base()}{self._REMOVE_AGENT_PATH}"
        log.info(
            "asio.remove_agent.start",
            endpoint_id=endpoint_id,
            url=url,
            client_id=client_id,
            site_id=site_id,
        )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # DELETE with a JSON body: httpx.delete() has no ``json=`` arg,
                # so use the generic request() to attach the payload.
                response = await client.request(
                    "DELETE",
                    url,
                    headers={**self._auth_headers(), "Content-Type": "application/json"},
                    json=json_body,
                )
                status = response.status_code
                body_preview = response.text[:500] if response.text else "(empty)"

                if status not in (200, 202, 204):
                    log.error(
                        "asio.remove_agent.bad_status",
                        endpoint_id=endpoint_id,
                        url=url,
                        status=status,
                        body=body_preview,
                    )
                    if status in (401, 403):
                        hint = (
                            "Asio rejected the uninstall (auth/scope). Ensure the "
                            "'platform.agent.delete' scope is in ASIO_SCOPE and the "
                            "OAuth client has it enabled in the Asio partner portal."
                        )
                    elif status == 404:
                        hint = (
                            "Asio returned 404 for the uninstall route. The endpoint "
                            "may already be removed, or the clientId/siteId did not "
                            "match this endpoint."
                        )
                    elif status == 400:
                        hint = (
                            "Asio rejected the uninstall body. Verify the endpoint "
                            "still exists and the clientId/siteId are correct."
                        )
                    else:
                        hint = ""
                    return {
                        "error": f"HTTP {status}: {body_preview[:200]}",
                        "hint": hint,
                        "endpoint_id": endpoint_id,
                    }

                # Surface per-endpoint failures the API reports with a 200.
                try:
                    payload = response.json()
                except Exception:  # noqa: BLE001
                    payload = None
                if isinstance(payload, dict):
                    for ep in payload.get("endpoints", []) or []:
                        ep_status = str(ep.get("uninstallStatus", "")).lower()
                        if ep_status and ep_status not in ("success", "succeeded", "ok"):
                            log.error(
                                "asio.remove_agent.endpoint_failed",
                                endpoint_id=endpoint_id,
                                uninstall_status=ep.get("uninstallStatus"),
                                message=ep.get("message"),
                            )
                            return {
                                "error": (
                                    f"Asio uninstall reported "
                                    f"{ep.get('uninstallStatus')}: {ep.get('message', '')}"
                                ),
                                "endpoint_id": endpoint_id,
                            }

                # Invalidate the devices cache so the removed agent is not served
                # from a stale cache on the next fetch.
                self._devices_cache = []
                self._devices_cache_ts = 0.0

                log.info(
                    "asio.remove_agent.success",
                    endpoint_id=endpoint_id,
                    url=url,
                    status=status,
                )
                return {
                    "status": "success",
                    "platform": self.platform_name,
                    "endpoint_id": endpoint_id,
                    "action": "agent_uninstalled",
                    "url": url,
                }
        except Exception as exc:  # noqa: BLE001
            log.error(
                "asio.remove_agent.exception",
                endpoint_id=endpoint_id,
                error=str(exc),
            )
            return {"error": str(exc), "endpoint_id": endpoint_id}

    #: Asio endpoint-heartbeat route used to detect online/offline availability.
    #: Per the ConnectWise Platform spec this is a ``GET`` whose query params
    #: identify the resources to inspect. Requires ``platform.devices.read``.
    _HEARTBEAT_PATH: str = "/platform/v2/device/endpoints/heartbeat"

    async def get_endpoint_heartbeat(
        self,
        resources: list[str],
        resource_type: str = "endpoints",
        offline_lookback: str | None = None,
        availability: bool | None = None,
    ) -> dict[str, Any]:
        """Return online/offline availability for the given resources.

        Calls ``GET /api/platform/v2/device/endpoints/heartbeat`` to detect
        which endpoints are currently online. This is the API-native
        replacement for the manual "last contact" CSV upload used elsewhere to
        flag stale/inactive devices. Requires the ``platform.devices.read``
        scope.

        Parameters
        ----------
        resources : list[str]
            Resource IDs to query. Limits per the spec: clients <= 4,
            sites <= 10, endpoints <= 500.
        resource_type : str
            One of ``clients``, ``sites``, or ``endpoints`` (default).
        offline_lookback : str, optional
            Return only endpoints offline for >= N days, formatted ``Nd``
            (e.g. ``7d``). Cannot be combined with ``availability``.
        availability : bool, optional
            Filter by availability: ``True`` = online, ``False`` = offline.
            Cannot be combined with ``offline_lookback``.

        Returns
        -------
        dict
            The ``EndpointHeartbeatResponse`` payload on success, or a dict with
            an ``error`` key on failure.
        """
        if not resources:
            return {"error": "resources is required to query Asio heartbeat"}
        if offline_lookback and availability is not None:
            return {
                "error": "offlineLookback and availability cannot be combined",
            }

        await self._ensure_auth()

        params: dict[str, Any] = {
            "resourceType": resource_type,
            "resources": ",".join(resources),
        }
        if offline_lookback:
            params["offlineLookback"] = offline_lookback
        if availability is not None:
            params["availability"] = "true" if availability else "false"

        url = f"{self._api_base()}{self._HEARTBEAT_PATH}"
        log.info(
            "asio.heartbeat.start",
            url=url,
            resource_type=resource_type,
            count=len(resources),
        )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await self._get_page(client, url, params)
                return response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            body_preview = exc.response.text[:300] if exc.response.text else "(empty)"
            log.error(
                "asio.heartbeat.bad_status",
                url=url,
                status=status,
                body=body_preview,
            )
            return {"error": f"HTTP {status}: {body_preview[:200]}"}
        except Exception as exc:  # noqa: BLE001
            log.error("asio.heartbeat.exception", url=url, error=str(exc))
            return {"error": str(exc)}
