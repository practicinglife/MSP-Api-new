"""ESET MSP Administrator 2 (EMA2) connector.

Authentication: POST /api/Token/Get  → Bearer JWT (access_token + refresh_token).
Company endpoints:
  - GET  /api/User/Current              → current user + companyId
  - POST /api/Company/Children          → paginated child companies
  - POST /api/Company/Descendants       → all descendants by entity type

The EMA2 API is a **license / company management** portal — it does NOT
expose individual device endpoints.  ``get_devices()`` therefore returns
an empty list.  ESET is used for *company-level presence* in the
reconciled view (i.e. "this managed company has ESET coverage").
"""

from typing import Any

import httpx
import structlog

from app.config import Settings, get_settings
from app.connectors.base import AbstractPlatformConnector
from app.models.company import CompanyRecord
from app.models.device import DeviceRecord
from app.services.normalizers import normalize_company_name

log = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_PAGE_SIZE = 100
_AUTH_MAX_RETRIES = 5
_AUTH_RETRY_DELAY = 3.0


class EsetConnector(AbstractPlatformConnector):
    """Connector for ESET MSP Administrator 2 (EMA2) portal.

    ESET install is handled via per-company Asio automation scripts (not
    the ESET API directly).  Each company has a pre-configured script in
    Asio that contains the ESET installer for that company.  The
    ``supports_install`` flag is set so the UI shows the Install badge.
    """

    platform_name = "eset"
    supports_install = True
    supports_company_create = True

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialise with injected or default settings."""
        self._settings = settings or get_settings()
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._company_id: str = ""

    def check_config(self) -> list[str]:
        missing = []
        if not self._settings.eset_username:
            missing.append("ESET_USERNAME")
        if not self._settings.eset_password:
            missing.append("ESET_PASSWORD")
        return missing

    def _base_url(self) -> str:
        return self._settings.eset_base_url.rstrip("/")

    async def authenticate(self) -> None:
        """Obtain Bearer JWT via POST /api/Token/Get (username + password).

        Retries up to ``_AUTH_MAX_RETRIES`` times on 5xx server errors with
        exponential backoff to handle transient ESET API failures.
        """
        import asyncio

        url = f"{self._base_url()}/api/Token/Get"
        last_exc: Exception | None = None

        for attempt in range(1, _AUTH_MAX_RETRIES + 1):
            log.info("eset.authenticate", url=url, attempt=attempt)
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    response = await client.post(
                        url,
                        json={
                            "username": self._settings.eset_username,
                            "password": self._settings.eset_password,
                        },
                    )
                    response.raise_for_status()
                    body: dict[str, Any] = response.json()
                    self._access_token = (
                        body.get("accessToken")
                        or body.get("access_token")
                        or body.get("Token")
                        or body.get("token")
                        or ""
                    )
                    self._refresh_token = (
                        body.get("refreshToken")
                        or body.get("refresh_token")
                        or ""
                    )
                    if not self._access_token:
                        log.error("eset.authenticate.no_token", response_keys=list(body.keys()))
                        raise RuntimeError("ESET authentication succeeded but no access token in response")
                    log.info("eset.authenticated")
                    return
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code >= 500 and attempt < _AUTH_MAX_RETRIES:
                    delay = _AUTH_RETRY_DELAY * (2 ** (attempt - 1))
                    log.warning(
                        "eset.authenticate.retry",
                        status=exc.response.status_code,
                        attempt=attempt,
                        next_delay=delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                # All retries exhausted — wrap with clear message
                log.error(
                    "eset.authenticate.failed",
                    status=exc.response.status_code,
                    attempts=_AUTH_MAX_RETRIES,
                    url=url,
                )
                raise RuntimeError(
                    f"ESET MSP Portal authentication failed after {_AUTH_MAX_RETRIES} attempts "
                    f"(HTTP {exc.response.status_code}). The ESET API may be temporarily "
                    f"unavailable. Reconciliation will continue without ESET data."
                ) from exc
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < _AUTH_MAX_RETRIES:
                    delay = _AUTH_RETRY_DELAY * (2 ** (attempt - 1))
                    log.warning(
                        "eset.authenticate.retry",
                        error=str(exc),
                        attempt=attempt,
                        next_delay=delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                log.error(
                    "eset.authenticate.failed",
                    error=str(exc),
                    attempts=_AUTH_MAX_RETRIES,
                    url=url,
                )
                raise RuntimeError(
                    f"ESET MSP Portal unreachable after {_AUTH_MAX_RETRIES} attempts "
                    f"({type(exc).__name__}). Reconciliation will continue without ESET data."
                ) from exc

        if last_exc:
            raise last_exc

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _ensure_auth(self) -> None:
        """Authenticate if we don't already have a token."""
        if not self._access_token:
            await self.authenticate()

    async def _reauthenticate(self) -> None:
        """Force a fresh token (clear cached token and re-authenticate)."""
        self._access_token = ""
        self._refresh_token = ""
        await self.authenticate()

    async def _get_my_company_id(self, client: httpx.AsyncClient) -> str:
        """Call GET /api/User/Current to discover our root companyId.

        Retries once on 401 by re-authenticating with a fresh token.
        """
        if self._company_id:
            return self._company_id

        for attempt in range(2):
            response = await client.get(
                f"{self._base_url()}/api/User/Current",
                headers=self._auth_headers(),
            )
            if response.status_code == 401 and attempt == 0:
                log.warning("eset.user_current.401_retry", detail="Token rejected, re-authenticating")
                await self._reauthenticate()
                continue
            response.raise_for_status()
            break

        body: dict[str, Any] = response.json()
        # The response includes company details; companyId is typically a GUID string
        self._company_id = str(
            body.get("companyId")
            or body.get("CompanyId")
            or body.get("company", {}).get("companyId", "")
            or body.get("company", {}).get("id", "")
            or ""
        )
        log.info("eset.my_company_id", company_id=self._company_id)
        return self._company_id

    def get_diagnostics(self) -> dict[str, Any]:
        """Return a diagnostic dict showing resolved config (for debugging)."""
        return {
            "base_url": self._base_url(),
            "token_url": f"{self._base_url()}/api/Token/Get",
            "user_current_url": f"{self._base_url()}/api/User/Current",
            "children_url": f"{self._base_url()}/api/Company/Children",
            "username_set": bool(self._settings.eset_username),
            "password_set": bool(self._settings.eset_password),
            "has_token": bool(self._access_token),
            "company_id": self._company_id,
        }

    async def get_companies(self) -> list[CompanyRecord]:
        """Return all managed child companies from the ESET EMA2 portal.

        Flow:
        1. GET /api/User/Current → get our root companyId
        2. POST /api/Company/Children (paginated) → list first-level children

        Retries once on 401 by re-authenticating with a fresh token.
        """
        await self._ensure_auth()

        records: list[CompanyRecord] = []
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            company_id = await self._get_my_company_id(client)
            if not company_id:
                log.warning("eset.get_companies.no_company_id")
                return records

            # Paginate through children
            skip = 0
            retried_401 = False
            while True:
                resp = await client.post(
                    f"{self._base_url()}/api/Company/Children",
                    headers={**self._auth_headers(), "Content-Type": "application/json"},
                    json={
                        "companyId": company_id,
                        "skip": skip,
                        "take": _PAGE_SIZE,
                    },
                )
                # Handle 401 — re-authenticate once and retry
                if resp.status_code == 401 and not retried_401:
                    retried_401 = True
                    log.warning(
                        "eset.get_companies.401_retry",
                        url=f"{self._base_url()}/api/Company/Children",
                        detail="Bearer token rejected, forcing re-authentication",
                    )
                    await self._reauthenticate()
                    # Also clear company_id in case it was stale
                    self._company_id = ""
                    company_id = await self._get_my_company_id(client)
                    continue

                resp.raise_for_status()
                body = resp.json()
                # API may return {"companies": [...]} or a bare list
                children: list[dict[str, Any]] = (
                    body.get("companies", body) if isinstance(body, dict) else body
                )
                if not children:
                    break

                for item in children:
                    name = item.get("name") or item.get("Name") or ""
                    cid = str(
                        item.get("companyId")
                        or item.get("CompanyId")
                        or item.get("publicId")
                        or item.get("id")
                        or item.get("Id")
                        or ""
                    )
                    records.append(
                        CompanyRecord(
                            platform=self.platform_name,
                            platform_id=cid,
                            name=normalize_company_name(name),
                            raw=item,
                        )
                    )

                if len(children) < _PAGE_SIZE:
                    break
                skip += _PAGE_SIZE

        log.info("eset.get_companies", count=len(records))
        return records

    async def get_devices(self) -> list[DeviceRecord]:
        """Return an empty list — EMA2 is a license portal with no device endpoint.

        ESET is used for company-level presence only.
        """
        log.info("eset.get_devices.not_supported", reason="EMA2 has no device endpoint")
        return []

    async def get_license_seats(self) -> list[dict[str, Any]]:
        """Return per-company ESET license seat usage.

        Tries several EMA2 license endpoints in order, falling back to
        the Company/Children raw payload.  The ESET portal CSV export
        shows Units in two formats:

        - ``N/M``    — N devices used out of M licensed  (e.g. ``75/110``)
        - ``N-Mon``  — N seats, renewal month Mon         (e.g. ``1-Feb``)

        Status is ``Normal`` or ``Overused``.
        """
        await self._ensure_auth()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            company_id = await self._get_my_company_id(client)
            if not company_id:
                log.warning("eset.get_license_seats.no_company_id")
                return []

            # Try EMA2 endpoints in order of likelihood
            for path, payload in [
                ("/api/License/List",            {"companyId": company_id, "skip": 0, "take": _PAGE_SIZE}),
                ("/api/CompanyLicense/GetList",  {"companyId": company_id, "skip": 0, "take": _PAGE_SIZE}),
                ("/api/License/GetList",         {"companyId": company_id, "skip": 0, "take": _PAGE_SIZE}),
                ("/api/Company/Licenses",        {"companyId": company_id}),
            ]:
                try:
                    resp = await client.post(
                        f"{self._base_url()}{path}",
                        headers={**self._auth_headers(), "Content-Type": "application/json"},
                        json=payload,
                    )
                    if resp.status_code == 404:
                        continue
                    if resp.status_code == 401:
                        await self._reauthenticate()
                        continue
                    resp.raise_for_status()
                    items = resp.json()
                    if isinstance(items, dict):
                        items = items.get("licenses") or items.get("items") or items.get("data") or []
                    if items:
                        log.info("eset.get_license_seats.endpoint", path=path, count=len(items))
                        return self._parse_license_items(items)
                except Exception:  # noqa: BLE001
                    continue

        # All endpoints failed — extract from Company/Children raw payload
        log.info("eset.get_license_seats.fallback", reason="no license endpoint responded")
        return await self._seats_from_companies()

    @staticmethod
    def _parse_units(units_str: str) -> tuple[int, int]:
        """Parse ESET Units field → (seats_used, seats_total).

        Formats seen in the portal CSV:
        - ``75/110``  → used=75,  total=110
        - ``1-Feb``   → used=0,   total=1   (renewal month suffix ignored)
        - ``12``      → used=0,   total=12
        - ``0/1``     → used=0,   total=1
        """
        s = str(units_str).strip()
        if "/" in s:
            parts = s.split("/", 1)
            try:
                return int(parts[0]), int(parts[1])
            except ValueError:
                return 0, 0
        if "-" in s:
            # e.g. "1-Feb" — take the number before the dash as total
            try:
                return 0, int(s.split("-", 1)[0])
            except ValueError:
                return 0, 0
        try:
            return 0, int(s)
        except ValueError:
            return 0, 0

    def _parse_license_items(self, items: list[dict]) -> list[dict[str, Any]]:
        """Map raw EMA2 license API records to the standard seat schema."""
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        for lic in items:
            company_name = (
                lic.get("companyName") or lic.get("CompanyName")
                or lic.get("company", {}).get("name", "")
                or str(lic.get("Company.Name") or "")
            )
            cid = str(
                lic.get("companyId") or lic.get("CompanyId")
                or lic.get("Company.CompanyId") or ""
            )
            # Deduplicate by company ID (two rows per company in portal export)
            dedup_key = cid or company_name
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            units_raw = str(
                lic.get("units") or lic.get("Units")
                or lic.get("seatCount") or lic.get("SeatCount")
                or lic.get("quantity") or ""
            )
            seats_used, seats_total = self._parse_units(units_raw)

            product = (
                lic.get("productName") or lic.get("ProductName")
                or lic.get("Product.Name") or lic.get("licenseTypeName")
                or "ESET"
            )
            status = (
                lic.get("status") or lic.get("Status")
                or lic.get("licenseStatus") or "Unknown"
            )
            results.append({
                "company": normalize_company_name(company_name) or company_name.lower(),
                "company_display": company_name,
                "eset_company_id": cid,
                "product": product,
                "seats_total": seats_total,
                "seats_used": seats_used,
                "seats_available": max(0, seats_total - seats_used),
                "overused": str(status).lower() == "overused",
                "status": status,
                "license_key": lic.get("publicKey") or lic.get("PublicKey") or "",
                "expiry_date": (
                    lic.get("expirationDate") or lic.get("ExpirationDate")
                    or lic.get("Validity") or lic.get("validity") or ""
                ),
            })
        return results

    async def _seats_from_companies(self) -> list[dict[str, Any]]:
        """Fallback: extract seat data from the raw Company/Children payload."""
        companies = await self.get_companies()
        results: list[dict[str, Any]] = []
        for c in companies:
            raw = c.raw or {}
            units_raw = str(
                raw.get("units") or raw.get("Units")
                or raw.get("seatCount") or raw.get("SeatCount")
                or raw.get("licensedDevicesCount") or ""
            )
            seats_used, seats_total = self._parse_units(units_raw)
            if seats_total == 0 and seats_used == 0:
                continue
            status = raw.get("status") or raw.get("Status") or "Unknown"
            results.append({
                "company": c.name,
                "company_display": raw.get("name") or raw.get("Name") or c.name,
                "eset_company_id": c.platform_id,
                "product": raw.get("productName") or raw.get("Product.Name") or "ESET",
                "seats_total": seats_total,
                "seats_used": seats_used,
                "seats_available": max(0, seats_total - seats_used),
                "overused": str(status).lower() == "overused",
                "status": status,
                "license_key": raw.get("publicKey") or raw.get("PublicKey") or "",
                "expiry_date": raw.get("expirationDate") or raw.get("Validity") or "",
            })
        return results

    async def create_company_on_platform(
        self, name: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Create a new company in ESET MSP Administrator 2.

        Uses POST /api/Company to create a child company.
        """
        await self._ensure_auth()
        url = f"{self._base_url()}/api/Company"

        payload: dict[str, Any] = {
            "Name": name,
            "CompanyType": 1,  # Standard company type
        }

        # Optional fields
        if kwargs.get("address"):
            payload["Address"] = kwargs["address"]
        if kwargs.get("phone"):
            payload["Phone"] = kwargs["phone"]

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
                company_id = str(
                    body.get("companyId")
                    or body.get("CompanyId")
                    or body.get("id")
                    or body.get("Id")
                    or ""
                )
                log.info("eset.create_company", name=name, id=company_id)
                return {
                    "platform": self.platform_name,
                    "id": company_id,
                    "name": body.get("name") or body.get("Name") or name,
                    "raw": body,
                }
        except httpx.HTTPStatusError as exc:
            log.error(
                "eset.create_company.error",
                status=exc.response.status_code,
                body=exc.response.text[:300],
            )
            return {"error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"}
        except Exception as exc:  # noqa: BLE001
            log.error("eset.create_company.exception", error=str(exc))
            return {"error": str(exc)}
