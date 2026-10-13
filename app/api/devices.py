"""Devices API router."""

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.connector_health import get_connectors
from app.db.repository import (
    CompanyAliasRepository,
    DeviceAvStatusRepository,
    PlatformDeviceRepository,
    ReconciliationRepository,
    get_session_factory,
)
from app.services.normalizers import normalize_company_name, normalize_hostname, resolve_with_prefix

router = APIRouter(prefix="/api/devices", tags=["devices"])


async def _get_session() -> AsyncSession:  # type: ignore[return]
    """Dependency that yields a database session."""
    factory = get_session_factory()
    async with factory() as session:
        yield session  # type: ignore[misc]


@router.get("", response_model=list[dict[str, Any]])
async def list_devices(session: AsyncSession = Depends(_get_session)) -> list[dict[str, Any]]:
    """Return all stored device match results."""
    repo = ReconciliationRepository(session)
    matches = await repo.list_device_matches()
    return [
        {
            "id": m.id,
            "platform_a": m.platform_a,
            "platform_id_a": m.platform_id_a,
            "hostname_a": m.hostname_a,
            "platform_b": m.platform_b,
            "platform_id_b": m.platform_id_b,
            "hostname_b": m.hostname_b,
            "score": m.score,
            "status": m.status,
            "reasons": m.reasons,
        }
        for m in matches
    ]


@router.get("/unmatched", response_model=list[dict[str, Any]])
async def unmatched_devices(
    session: AsyncSession = Depends(_get_session),
) -> list[dict[str, Any]]:
    """Return devices that exist on one platform but are missing from others.

    Cross-references devices across all active connectors using:
    1. MAC address overlap (primary)
    2. Hostname + company name match (fallback)

    Company aliases are applied so merged companies are treated as one.
    Only devices with no match on at least one other platform are returned.
    """
    connectors = get_connectors()
    if not connectors:
        return []

    # Load alias map from DB
    alias_repo = CompanyAliasRepository(session)
    alias_map = await alias_repo.get_alias_map()

    # Fetch devices from all connectors concurrently
    async def _safe_fetch(connector):  # type: ignore[no-untyped-def]
        try:
            devices = await connector.get_devices()
            return connector.platform_name, devices
        except Exception:  # noqa: BLE001
            return connector.platform_name, []

    results = await asyncio.gather(*[_safe_fetch(c) for c in connectors])

    # Organise devices by platform
    platform_devices: dict[str, list] = {}
    all_platforms: list[str] = []
    for platform, devices in results:
        if devices:
            platform_devices[platform] = devices
            all_platforms.append(platform)

    if len(platform_devices) < 2:
        return []

    # Collect all normalised company names across platforms for prefix resolution
    _all_norm_names: set[str] = set()
    for _plat, devs in platform_devices.items():
        for d in devs:
            raw = d.company_name or d.company_platform_id or ""
            if raw:
                n = normalize_company_name(raw)
                if n:
                    _all_norm_names.add(alias_map.get(n, n))
    _canonical_names = sorted(_all_norm_names)

    def _resolve_company(device) -> str:  # type: ignore[no-untyped-def]
        raw = device.company_name or device.company_platform_id or ""
        norm = normalize_company_name(raw) if raw else ""
        norm = alias_map.get(norm, norm)
        return resolve_with_prefix(norm, _canonical_names)

    # Build lookup indices per platform for matching
    mac_to_platforms: dict[str, set[str]] = {}
    hostco_to_platforms: dict[tuple[str, str], set[str]] = {}

    for platform, devices in platform_devices.items():
        for device in devices:
            for mac in device.mac_addresses:
                if mac:
                    mac_to_platforms.setdefault(mac, set()).add(platform)
            hostname = device.hostname
            company = _resolve_company(device)
            if hostname and company:
                hostco_to_platforms.setdefault((hostname, company), set()).add(platform)

    # For each device, determine which platforms it's found on
    unmatched: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    seen_hostco: set[tuple[str, str]] = set()  # dedup same device emitted from multiple platforms

    for platform, devices in platform_devices.items():
        for device in devices:
            hostname = device.hostname
            company = _resolve_company(device)

            if not hostname:
                continue

            dedup_key = (platform, hostname, company)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            # Find all platforms this device appears on
            found_on: set[str] = {platform}

            # Check MAC matches
            for mac in device.mac_addresses:
                if mac and mac in mac_to_platforms:
                    found_on |= mac_to_platforms[mac]

            # Check hostname+company matches
            if hostname and company:
                key = (hostname, company)
                if key in hostco_to_platforms:
                    found_on |= hostco_to_platforms[key]

            # Determine missing platforms
            missing = [p for p in all_platforms if p not in found_on]

            if missing:
                # Skip if we've already emitted this (hostname, company) pair
                # from a different source platform with the same found_on/missing
                hostco_key = (hostname, company)
                if hostco_key in seen_hostco:
                    continue
                seen_hostco.add(hostco_key)

                unmatched.append({
                    "hostname": hostname,
                    "company": company,
                    "platform": platform,
                    "found_on": sorted(found_on),
                    "missing_from": sorted(missing),
                    "mac_addresses": device.mac_addresses[:3],
                    "serial_number": device.serial_number,
                })

    # Sort by company then hostname
    unmatched.sort(key=lambda d: (d["company"], d["hostname"]))
    return unmatched


@router.get("/{match_id}", response_model=dict[str, Any])
async def get_device(
    match_id: str, session: AsyncSession = Depends(_get_session)
) -> dict[str, Any]:
    """Return a single device match by ID."""
    repo = ReconciliationRepository(session)
    matches = await repo.list_device_matches()
    for m in matches:
        if m.id == match_id:
            return {
                "id": m.id,
                "platform_a": m.platform_a,
                "platform_id_a": m.platform_id_a,
                "hostname_a": m.hostname_a,
                "platform_b": m.platform_b,
                "platform_id_b": m.platform_id_b,
                "hostname_b": m.hostname_b,
                "score": m.score,
                "status": m.status,
                "reasons": m.reasons,
            }
    raise HTTPException(status_code=404, detail="Device match not found")


# ── AV Detection & Status Endpoints ──────────────────────────────


def _load_detect_script(callback_url: str = "") -> str:
    """Load the PowerShell AV detection script with callback URL injected."""
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "scripts", "detect_av.ps1"
    )
    with open(script_path, encoding="utf-8") as f:
        script = f.read()
    if callback_url:
        script = script.replace("{{CALLBACK_URL}}", callback_url)
    return script


@router.post("/av-detect", response_model=dict[str, Any])
async def trigger_av_detection(
    request: Request,
    session: AsyncSession = Depends(_get_session),
    company: str | None = None,
) -> dict[str, Any]:
    """Trigger AV detection on endpoints via ScreenConnect command execution.

    Sends the PowerShell detection script to ScreenConnect sessions.
    If ``company`` is specified, only sessions for that company are targeted.
    Otherwise targets all active sessions.

    The script detects installed AV products (ESET, SentinelOne, Defender, etc.)
    and POSTs results back to ``/api/devices/av-report``.

    Returns a summary of how many sessions were commanded.
    """
    import structlog as _structlog

    _log = _structlog.get_logger("devices.av_detect")

    sc_connector = None
    for c in get_connectors():
        if c.platform_name == "screenconnect":
            sc_connector = c
            break

    if sc_connector is None:
        raise HTTPException(status_code=503, detail="ScreenConnect connector not available")

    base_url = str(request.base_url).rstrip("/")
    callback_url = f"{base_url}/api/devices/av-report"
    script = _load_detect_script(callback_url)

    sessions = await sc_connector._fetch_sessions()
    if not sessions:
        return {"status": "no_sessions", "total": 0, "sent": 0}

    if company:
        norm_company = normalize_company_name(company)
        alias_repo = CompanyAliasRepository(session)
        alias_map = await alias_repo.get_alias_map()
        canonical = alias_map.get(norm_company, norm_company)

        filtered = []
        for s in sessions:
            cp_vals = s.get("CustomPropertyValues", [])
            raw_co = str(cp_vals[0]) if cp_vals else ""
            norm = normalize_company_name(raw_co)
            session_canonical = alias_map.get(norm, norm)
            if session_canonical == canonical:
                filtered.append(s)
        sessions = filtered

    session_ids = [str(s.get("SessionID", "")) for s in sessions if s.get("SessionID")]
    if not session_ids:
        return {"status": "no_matching_sessions", "total": 0, "sent": 0}

    batch_size = 50
    total_sent = 0
    total_failed = 0
    all_errors: list[dict[str, str]] = []

    for i in range(0, len(session_ids), batch_size):
        batch = session_ids[i : i + batch_size]
        result = await sc_connector.run_command(batch, script)
        if result:
            total_sent += result["sent"]
            total_failed += result["failed"]
            all_errors.extend(result.get("errors", []))

    _log.info(
        "av_detect.triggered",
        total_sessions=len(session_ids),
        sent=total_sent,
        failed=total_failed,
        company_filter=company,
    )

    return {
        "status": "ok",
        "total_sessions": len(session_ids),
        "sent": total_sent,
        "failed": total_failed,
        "errors": all_errors[:20],
        "company_filter": company,
        "callback_url": callback_url,
    }


@router.post("/av-report", response_model=dict[str, Any])
async def receive_av_report(
    request: Request,
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Receive AV detection results from remote endpoints.

    Called by the PowerShell detection script running on managed endpoints.
    Accepts JSON body::

        {
            "hostname": "DESKTOP-ABC123",
            "timestamp": "2025-01-15T10:30:00Z",
            "av_products": [
                {
                    "vendor": "eset",
                    "product": "ESET Endpoint Security",
                    "version": "12.0.2058.0",
                    "enabled": true,
                    "up_to_date": true,
                    "source": "SecurityCenter"
                }
            ]
        }
    """
    import structlog as _structlog

    _log = _structlog.get_logger("devices.av_report")

    body = await request.json()
    hostname = body.get("hostname", "")
    if not hostname:
        raise HTTPException(status_code=400, detail="hostname is required")

    hostname_norm = normalize_hostname(hostname)
    av_products = body.get("av_products", [])

    if not av_products:
        _log.info("av_report.no_products", hostname=hostname)
        return {"status": "ok", "hostname": hostname, "products_stored": 0}

    device_repo = PlatformDeviceRepository(session)
    company_canonical = ""
    for platform in ("asio", "connectwise_psa", "screenconnect", "sentinelone"):
        stored = await device_repo.get_stored_hostnames(platform)
        if hostname_norm in stored:
            company_canonical = stored[hostname_norm]
            break

    rows: list[dict[str, Any]] = []
    for product in av_products:
        vendor = product.get("vendor", "unknown")
        rows.append({
            "hostname": hostname,
            "hostname_norm": hostname_norm,
            "endpoint_id": "",
            "company_canonical": company_canonical,
            "av_vendor": vendor,
            "av_product": product.get("product", ""),
            "av_version": product.get("version", ""),
            "av_status": "installed",
            "av_enabled": product.get("enabled"),
            "av_up_to_date": product.get("up_to_date"),
            "detection_source": "screenconnect_command",
            "detected_at": datetime.now(UTC),
        })

    av_repo = DeviceAvStatusRepository(session)
    result = await av_repo.bulk_upsert(rows)

    _log.info(
        "av_report.stored",
        hostname=hostname,
        products=len(rows),
        inserted=result["inserted"],
        updated=result["updated"],
    )

    return {
        "status": "ok",
        "hostname": hostname,
        "products_stored": len(rows),
        "inserted": result["inserted"],
        "updated": result["updated"],
    }


@router.get("/av-status", response_model=dict[str, Any])
async def get_av_status(
    session: AsyncSession = Depends(_get_session),
    company: str | None = None,
) -> dict[str, Any]:
    """Return per-device AV status from all detection sources.

    Optionally filter by ``company`` name.
    """
    av_repo = DeviceAvStatusRepository(session)

    if company:
        norm = normalize_company_name(company)
        alias_repo = CompanyAliasRepository(session)
        alias_map = await alias_repo.get_alias_map()
        canonical = alias_map.get(norm, norm)
        records = await av_repo.list_by_company(canonical)
    else:
        records = await av_repo.list_all()

    stats = await av_repo.get_stats()

    by_host: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        entry = {
            "av_vendor": r.av_vendor,
            "av_product": r.av_product,
            "av_version": r.av_version,
            "av_status": r.av_status,
            "av_enabled": r.av_enabled,
            "av_up_to_date": r.av_up_to_date,
            "detection_source": r.detection_source,
            "detected_at": r.detected_at.isoformat() if r.detected_at else None,
        }
        key = r.hostname or r.hostname_norm
        by_host.setdefault(key, []).append(entry)

    return {
        "stats": stats,
        "devices": by_host,
        "total_devices": len(by_host),
    }


@router.post("/av-import", response_model=dict[str, Any])
async def import_av_results(
    request: Request,
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Bulk import AV detection results from any source.

    Accepts JSON body::

        {
            "source": "asio_group",
            "records": [
                {
                    "hostname": "NUC",
                    "av_vendor": "eset",
                    "av_product": "ESET Endpoint Security",
                    "av_version": "12.0.2058.0"
                }
            ]
        }

    Supported sources: ``asio_group``, ``sentinelone_apps``,
    ``eset_protect``, ``csv_upload``, ``manual``.
    """
    body = await request.json()
    source = body.get("source", "manual")
    records = body.get("records", [])

    if not records:
        return {"status": "ok", "imported": 0}

    rows: list[dict[str, Any]] = []
    for rec in records:
        hostname = rec.get("hostname", "")
        if not hostname:
            continue
        rows.append({
            "hostname": hostname,
            "hostname_norm": normalize_hostname(hostname),
            "endpoint_id": rec.get("endpoint_id", ""),
            "company_canonical": rec.get("company_canonical", ""),
            "av_vendor": rec.get("av_vendor", "unknown"),
            "av_product": rec.get("av_product", ""),
            "av_version": rec.get("av_version", ""),
            "av_status": rec.get("av_status", "installed"),
            "av_enabled": rec.get("av_enabled"),
            "av_up_to_date": rec.get("av_up_to_date"),
            "detection_source": source,
            "detected_at": datetime.now(UTC),
        })

    av_repo = DeviceAvStatusRepository(session)
    result = await av_repo.bulk_upsert(rows)

    return {
        "status": "ok",
        "source": source,
        "imported": len(rows),
        "inserted": result["inserted"],
        "updated": result["updated"],
    }


@router.post("/av-scan-asio", response_model=dict[str, Any])
async def scan_asio_av_applications(
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Scan ASIO endpoints for installed AV products via the applications API.

    Uses ``POST /api/platform/v2/device/endpoints/applications`` **without**
    a server-side name filter (it returns 404) and identifies AV products
    client-side using publisher + application-name matching.

    Detected vendors and their identification criteria:

    * **ESET** — publisher ``"ESET, spol. s r.o."`` or app name starting
      with ``"ESET"``
    * **SentinelOne** — publisher contains ``"SentinelOne"``
    * **Bitdefender** — publisher ``"Bitdefender SRL"`` or starts with
      ``"Bitdefender"``
    * **Malwarebytes** — publisher contains ``"Malwarebytes"``
    * **McAfee** — publisher contains ``"McAfee"``
    * **Windows Defender** — app name containing ``"Windows Defender"`` or
      ``"Microsoft Defender"``

    All ASIO endpoint IDs are loaded from ``platform_devices`` (populated by
    ``POST /api/companies/scan``).  Results are stored in
    ``device_av_status`` with ``detection_source='asio_applications'``.

    .. note::

       The ASIO applications API may not list all security products (e.g.
       ESET Endpoint Security often doesn't appear in standard app
       inventory).  Use ASIO device groups (``platform.deviceGroups.read``
       scope) for comprehensive ESET coverage.
    """
    import structlog as _structlog

    _log = _structlog.get_logger("devices.av_scan_asio")

    # ── AV vendor identification rules ──
    # Each entry: (vendor_key, match function that receives (app_name_lower, publisher_lower))
    def _is_eset(name: str, pub: str) -> bool:
        return "eset, spol" in pub or name.startswith("eset")

    def _is_sentinelone(name: str, pub: str) -> bool:
        return "sentinelone" in pub or name.startswith("sentinelone")

    def _is_bitdefender(name: str, pub: str) -> bool:
        return "bitdefender" in pub or name.startswith("bitdefender")

    def _is_malwarebytes(name: str, pub: str) -> bool:
        return "malwarebytes" in pub or name.startswith("malwarebytes")

    def _is_mcafee(name: str, pub: str) -> bool:
        return "mcafee" in pub or name.startswith("mcafee")

    def _is_defender(name: str, pub: str) -> bool:
        return "windows defender" in name or "microsoft defender" in name

    vendor_rules: list[tuple[str, Any]] = [
        ("eset", _is_eset),
        ("sentinelone", _is_sentinelone),
        ("bitdefender", _is_bitdefender),
        ("malwarebytes", _is_malwarebytes),
        ("mcafee", _is_mcafee),
        ("defender", _is_defender),
    ]

    # Get ASIO connector
    asio_connector = None
    for c in get_connectors():
        if c.platform_name == "asio":
            asio_connector = c
            break

    if asio_connector is None:
        raise HTTPException(status_code=503, detail="ASIO connector not available")

    # Load all ASIO endpoint IDs from DB
    device_repo = PlatformDeviceRepository(session)
    eid_map = await device_repo.get_stored_endpoint_ids("asio")

    if not eid_map:
        return {
            "status": "no_endpoints",
            "detail": "No ASIO endpoints in DB. Run POST /api/companies/scan first.",
            "total_endpoints": 0,
            "av_found": 0,
        }

    endpoint_ids = list(eid_map.keys())
    _log.info("av_scan_asio.start", total_endpoints=len(endpoint_ids))

    # Query ASIO for ALL applications (no server-side filter — it causes 404)
    results = await asio_connector.get_endpoint_applications(endpoint_ids)

    # Build endpoint_id → hostname lookup from DB
    all_asio_devices = await device_repo.list_all_by_platform("asio")
    eid_to_hostname: dict[str, str] = {}
    eid_to_hostname_norm: dict[str, str] = {}
    for dev in all_asio_devices:
        if dev.endpoint_id:
            eid_to_hostname[dev.endpoint_id] = dev.hostname
            eid_to_hostname_norm[dev.endpoint_id] = dev.hostname_norm

    # Parse results — identify AV products client-side
    rows: list[dict[str, Any]] = []
    av_hits: list[dict[str, Any]] = []
    vendor_counts: dict[str, int] = {}
    vendor_endpoint_sets: dict[str, set[str]] = {}

    for entry in results:
        ep_id = str(entry.get("endpointID", ""))
        company_canonical = eid_map.get(ep_id, "")
        hostname = eid_to_hostname.get(ep_id, "")
        hostname_norm = eid_to_hostname_norm.get(ep_id, "")

        for app in entry.get("applications", []):
            app_name = app.get("name", "") or ""
            publisher = app.get("publisher", "") or ""
            app_name_lower = app_name.lower()
            publisher_lower = publisher.lower()

            # Check against each vendor rule
            matched_vendor: str | None = None
            for vendor_key, match_fn in vendor_rules:
                if match_fn(app_name_lower, publisher_lower):
                    matched_vendor = vendor_key
                    break

            if not matched_vendor:
                continue

            version = app.get("version", "")
            last_access = app.get("lastAccessDateTime", "")

            # Parse lastAccessDateTime for detected_at
            detected_at = datetime.now(UTC)
            if last_access:
                try:
                    detected_at = datetime.fromisoformat(
                        last_access.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    pass

            rows.append({
                "hostname": hostname or ep_id,
                "hostname_norm": hostname_norm or normalize_hostname(ep_id),
                "endpoint_id": ep_id,
                "company_canonical": company_canonical,
                "av_vendor": matched_vendor,
                "av_product": app_name,
                "av_version": version,
                "av_status": "installed",
                "av_enabled": True,
                "av_up_to_date": None,
                "detection_source": "asio_applications",
                "detected_at": detected_at,
            })

            vendor_counts[matched_vendor] = vendor_counts.get(matched_vendor, 0) + 1
            vendor_endpoint_sets.setdefault(matched_vendor, set()).add(ep_id)

            av_hits.append({
                "endpoint_id": ep_id,
                "hostname": hostname,
                "company": company_canonical,
                "vendor": matched_vendor,
                "product": app_name,
                "version": version,
                "publisher": publisher,
                "last_access": last_access,
            })

    _log.info(
        "av_scan_asio.results",
        total_endpoints=len(endpoint_ids),
        endpoints_with_apps=len(results),
        av_records=len(rows),
        vendor_counts=vendor_counts,
    )

    # Store in DB
    upsert_result = {"inserted": 0, "updated": 0}
    if rows:
        av_repo = DeviceAvStatusRepository(session)
        upsert_result = await av_repo.bulk_upsert(rows)

    # Build per-vendor summary
    vendor_summary: dict[str, dict[str, Any]] = {}
    for vendor_key in sorted(vendor_counts.keys()):
        vendor_summary[vendor_key] = {
            "app_records": vendor_counts[vendor_key],
            "unique_endpoints": len(vendor_endpoint_sets.get(vendor_key, set())),
        }

    return {
        "status": "ok",
        "total_endpoints_queried": len(endpoint_ids),
        "endpoints_with_apps": len(results),
        "av_records_found": len(rows),
        "inserted": upsert_result["inserted"],
        "updated": upsert_result["updated"],
        "vendors": vendor_summary,
        "sample": av_hits[:30],
    }


@router.post("/av-scan-asio-groups", response_model=dict[str, Any])
async def scan_asio_device_groups(
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Detect ESET endpoints via ASIO device groups and fetch version details.

    Requires the ``platform.deviceGroups.read`` OAuth scope on the ASIO
    client.  The endpoint:

    1. Lists all ASIO device groups.
    2. Identifies groups whose name contains ``"eset"`` (case-insensitive).
    3. Fetches all endpoint IDs in those groups.
    4. Marks every endpoint as ESET-installed in ``device_av_status``
       with ``detection_source='asio_device_group'``.
    5. Queries the applications API for those endpoints to capture the
       specific ESET product name and version (e.g. *ESET Endpoint
       Security 12.1.2057.3*, *ESET Management Agent 13.1.1110.0*).

    If the scope is not yet configured the response will indicate that
    with ``status='scope_not_configured'``.
    """
    import structlog as _structlog

    _log = _structlog.get_logger("devices.av_scan_asio_groups")

    # Get ASIO connector
    asio_connector = None
    for c in get_connectors():
        if c.platform_name == "asio":
            asio_connector = c
            break

    if asio_connector is None:
        raise HTTPException(status_code=503, detail="ASIO connector not available")

    # Step 1: List device groups
    groups = await asio_connector.get_device_groups()
    if not groups:
        return {
            "status": "scope_not_configured",
            "detail": (
                "No device groups returned. Add platform.deviceGroups.read "
                "scope in the ConnectWise developer portal, then re-authenticate."
            ),
            "groups_found": 0,
            "eset_endpoints": 0,
        }

    # Step 2: Find ESET-related groups
    eset_groups: list[dict[str, Any]] = []
    for g in groups:
        name = (g.get("name", "") or g.get("groupName", "") or "").lower()
        if "eset" in name:
            eset_groups.append(g)

    _log.info(
        "av_scan_asio_groups.groups",
        total_groups=len(groups),
        eset_groups=len(eset_groups),
        eset_names=[g.get("name", g.get("groupName", "")) for g in eset_groups],
    )

    if not eset_groups:
        return {
            "status": "no_eset_groups",
            "detail": "No device groups with 'ESET' in the name were found.",
            "total_groups": len(groups),
            "all_groups": [
                {"id": g.get("id", g.get("groupId", "")), "name": g.get("name", g.get("groupName", ""))}
                for g in groups
            ],
            "eset_endpoints": 0,
        }

    # Step 3: Get endpoint IDs from all ESET groups
    all_group_endpoints: list[dict[str, Any]] = []
    group_details: list[dict[str, Any]] = []
    seen_eids: set[str] = set()

    for g in eset_groups:
        gid = str(g.get("id", g.get("groupId", "")))
        gname = g.get("name", g.get("groupName", ""))
        endpoints = await asio_connector.get_devices_in_group(gid)
        group_details.append({"id": gid, "name": gname, "endpoint_count": len(endpoints)})
        for ep in endpoints:
            eid = str(ep.get("endpointID", ep.get("endpointId", ep.get("id", ""))))
            if eid and eid not in seen_eids:
                seen_eids.add(eid)
                all_group_endpoints.append(ep)

    _log.info(
        "av_scan_asio_groups.endpoints",
        unique_endpoints=len(all_group_endpoints),
    )

    if not all_group_endpoints:
        return {
            "status": "no_endpoints_in_groups",
            "detail": "ESET groups found but contain no endpoints.",
            "groups": group_details,
            "eset_endpoints": 0,
        }

    # Step 4: Cross-reference with platform_devices for hostname lookup
    device_repo = PlatformDeviceRepository(session)
    eid_map = await device_repo.get_stored_endpoint_ids("asio")
    all_asio_devices = await device_repo.list_all_by_platform("asio")
    eid_to_hostname: dict[str, str] = {}
    eid_to_hostname_norm: dict[str, str] = {}
    for dev in all_asio_devices:
        if dev.endpoint_id:
            eid_to_hostname[dev.endpoint_id] = dev.hostname
            eid_to_hostname_norm[dev.endpoint_id] = dev.hostname_norm

    # Step 5: Query applications API for ESET version details on these endpoints
    group_eids = list(seen_eids)
    app_results = await asio_connector.get_endpoint_applications(group_eids)

    # Build per-endpoint ESET application details
    eset_app_details: dict[str, list[dict[str, Any]]] = {}
    for entry in app_results:
        ep_id = str(entry.get("endpointID", ""))
        for app in entry.get("applications", []):
            app_name = app.get("name", "") or ""
            publisher = (app.get("publisher", "") or "").lower()
            app_name_lower = app_name.lower()
            is_eset = "eset, spol" in publisher or app_name_lower.startswith("eset")
            if is_eset:
                eset_app_details.setdefault(ep_id, []).append({
                    "product": app_name,
                    "version": app.get("version", ""),
                    "publisher": app.get("publisher", ""),
                    "last_access": app.get("lastAccessDateTime", ""),
                })

    # Step 6: Build DB rows — every endpoint in the ESET group is ESET-installed,
    # even if the applications API doesn't return specific product details.
    rows: list[dict[str, Any]] = []
    endpoint_summary: list[dict[str, Any]] = []

    for eid in group_eids:
        hostname = eid_to_hostname.get(eid, "")
        hostname_norm = eid_to_hostname_norm.get(eid, "")
        company = eid_map.get(eid, "")
        apps = eset_app_details.get(eid, [])

        if apps:
            # Store each ESET product found via applications API
            for app_info in apps:
                detected_at = datetime.now(UTC)
                if app_info.get("last_access"):
                    try:
                        detected_at = datetime.fromisoformat(
                            app_info["last_access"].replace("Z", "+00:00")
                        )
                    except (ValueError, AttributeError):
                        pass

                rows.append({
                    "hostname": hostname or eid,
                    "hostname_norm": hostname_norm or normalize_hostname(eid),
                    "endpoint_id": eid,
                    "company_canonical": company,
                    "av_vendor": "eset",
                    "av_product": app_info["product"],
                    "av_version": app_info["version"],
                    "av_status": "installed",
                    "av_enabled": True,
                    "av_up_to_date": None,
                    "detection_source": "asio_device_group",
                    "detected_at": detected_at,
                })

                endpoint_summary.append({
                    "endpoint_id": eid,
                    "hostname": hostname,
                    "company": company,
                    "product": app_info["product"],
                    "version": app_info["version"],
                    "source": "applications_api",
                })
        else:
            # Group membership confirms ESET but no app details available
            rows.append({
                "hostname": hostname or eid,
                "hostname_norm": hostname_norm or normalize_hostname(eid),
                "endpoint_id": eid,
                "company_canonical": company,
                "av_vendor": "eset",
                "av_product": "ESET (group membership)",
                "av_version": "",
                "av_status": "installed",
                "av_enabled": True,
                "av_up_to_date": None,
                "detection_source": "asio_device_group",
                "detected_at": datetime.now(UTC),
            })

            endpoint_summary.append({
                "endpoint_id": eid,
                "hostname": hostname,
                "company": company,
                "product": "ESET (group membership)",
                "version": "",
                "source": "group_only",
            })

    # Store in DB
    upsert_result = {"inserted": 0, "updated": 0}
    if rows:
        av_repo = DeviceAvStatusRepository(session)
        upsert_result = await av_repo.bulk_upsert(rows)

    _log.info(
        "av_scan_asio_groups.done",
        groups=len(eset_groups),
        unique_endpoints=len(group_eids),
        with_version=len(eset_app_details),
        without_version=len(group_eids) - len(eset_app_details),
        inserted=upsert_result["inserted"],
        updated=upsert_result["updated"],
    )

    return {
        "status": "ok",
        "groups": group_details,
        "total_eset_endpoints": len(group_eids),
        "with_version_details": len(eset_app_details),
        "without_version_details": len(group_eids) - len(eset_app_details),
        "db_inserted": upsert_result["inserted"],
        "db_updated": upsert_result["updated"],
        "sample": endpoint_summary[:30],
    }


@router.post("/av-scan-eset-connect", response_model=dict[str, Any])
async def scan_eset_connect_devices(
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Fetch per-device ESET product versions from ESET Connect (Protect Cloud).

    Authenticates with the ESET Connect API via OAuth password grant and
    queries ``GET /v1/devices`` for all managed devices.  For each device
    the ``deployedComponents`` list is parsed to extract ESET product names
    and version strings (e.g. *ESET Endpoint Security 12.1.2057.3*).

    Results are cross-referenced against ``platform_devices`` by hostname to
    link each ESET record to its canonical company name and endpoint ID.

    All records are stored in ``device_av_status`` with
    ``detection_source='eset_connect'``.

    This is the **definitive** source for per-device ESET version details —
    the ASIO applications API typically only finds a few ESET entries.
    """
    import structlog as _structlog

    _log = _structlog.get_logger("devices.av_scan_eset_connect")

    from app.connectors.eset_connect import EsetConnectConnector

    from app.services.builtin_integrations import detail_connector

    # Credentials saved in the app first, then .env
    connector = await detail_connector("eset_connect") or EsetConnectConnector()

    missing = connector.check_config()
    if missing:
        return {
            "status": "not_configured",
            "detail": f"Missing ESET Connect configuration: {', '.join(missing)}",
            "missing": missing,
        }

    authenticated = await connector.authenticate()
    if not authenticated:
        return {
            "status": "auth_failed",
            "detail": "Failed to authenticate with ESET Connect API.",
        }

    devices = await connector.get_devices()
    if not devices:
        return {
            "status": "no_devices",
            "detail": "ESET Connect returned no devices.",
            "total_devices": 0,
        }

    _log.info("eset_connect_scan.fetched", total_devices=len(devices))

    # Build hostname lookup from platform_devices for cross-referencing
    device_repo = PlatformDeviceRepository(session)
    hostname_map: dict[str, tuple[str, str]] = {}
    for platform in ("asio", "connectwise_psa", "screenconnect", "sentinelone"):
        stored = await device_repo.get_stored_hostnames(platform)
        for hn, company in stored.items():
            if hn not in hostname_map:
                hostname_map[hn] = (company, "")

    # Also get endpoint IDs from ASIO for richer cross-referencing
    all_asio_devices = await device_repo.list_all_by_platform("asio")
    asio_hostname_to_eid: dict[str, str] = {}
    for dev in all_asio_devices:
        if dev.hostname_norm and dev.endpoint_id:
            asio_hostname_to_eid[dev.hostname_norm] = dev.endpoint_id

    rows: list[dict[str, Any]] = []
    device_summary: list[dict[str, Any]] = []
    devices_with_components = 0
    devices_with_products = 0

    for device in devices:
        display_name = device.get("displayName", "")
        uuid = device.get("uuid", "")
        last_sync = device.get("lastSyncTime", "")
        os_info = device.get("operatingSystem", "")

        hostname_norm = normalize_hostname(display_name) if display_name else ""

        # Cross-reference with platform_devices
        company_canonical = ""
        endpoint_id = ""
        if hostname_norm and hostname_norm in hostname_map:
            company_canonical = hostname_map[hostname_norm][0]
        if hostname_norm and hostname_norm in asio_hostname_to_eid:
            endpoint_id = asio_hostname_to_eid[hostname_norm]

        # Parse deployedComponents for ESET product versions
        components = EsetConnectConnector.extract_eset_versions(device)
        active_products = EsetConnectConnector.extract_active_products(device)

        if components:
            devices_with_components += 1
        if active_products:
            devices_with_products += 1

        # Parse lastSyncTime for detected_at
        detected_at = datetime.now(UTC)
        if last_sync:
            try:
                detected_at = datetime.fromisoformat(
                    last_sync.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass

        if components:
            for comp in components:
                rows.append({
                    "hostname": display_name or uuid,
                    "hostname_norm": hostname_norm or normalize_hostname(uuid),
                    "endpoint_id": endpoint_id,
                    "company_canonical": company_canonical,
                    "av_vendor": "eset",
                    "av_product": comp["product"],
                    "av_version": comp["version"],
                    "av_status": "installed",
                    "av_enabled": True,
                    "av_up_to_date": None,
                    "detection_source": "eset_connect",
                    "detected_at": detected_at,
                })
        else:
            # Device is in ESET Protect but no component details — still record it
            product_name = "ESET (managed device)"
            if active_products:
                product_name = active_products[0].get("name", product_name)
            rows.append({
                "hostname": display_name or uuid,
                "hostname_norm": hostname_norm or normalize_hostname(uuid),
                "endpoint_id": endpoint_id,
                "company_canonical": company_canonical,
                "av_vendor": "eset",
                "av_product": product_name,
                "av_version": "",
                "av_status": "installed",
                "av_enabled": True,
                "av_up_to_date": None,
                "detection_source": "eset_connect",
                "detected_at": detected_at,
            })

        device_summary.append({
            "display_name": display_name,
            "uuid": uuid,
            "hostname_norm": hostname_norm,
            "company": company_canonical,
            "os": os_info,
            "last_sync": last_sync,
            "components": [f"{c['product']} {c['version']}" for c in components],
            "active_products": [p["name"] for p in active_products],
            "matched": bool(company_canonical),
        })

    # Store in DB
    upsert_result = {"inserted": 0, "updated": 0}
    if rows:
        av_repo = DeviceAvStatusRepository(session)
        upsert_result = await av_repo.bulk_upsert(rows)

    matched_count = sum(1 for d in device_summary if d["matched"])

    _log.info(
        "eset_connect_scan.done",
        total_devices=len(devices),
        with_components=devices_with_components,
        with_products=devices_with_products,
        av_records=len(rows),
        matched_to_platform=matched_count,
        inserted=upsert_result["inserted"],
        updated=upsert_result["updated"],
    )

    return {
        "status": "ok",
        "total_eset_devices": len(devices),
        "with_component_details": devices_with_components,
        "with_active_products": devices_with_products,
        "av_records_stored": len(rows),
        "matched_to_platform_devices": matched_count,
        "unmatched": len(devices) - matched_count,
        "db_inserted": upsert_result["inserted"],
        "db_updated": upsert_result["updated"],
        "sample": device_summary[:30],
    }
