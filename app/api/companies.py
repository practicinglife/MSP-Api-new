"""Companies API router."""

import asyncio
import csv
import html
import io
import re
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.connector_health import get_connectors
from app.auth.write_mode import enforce_direct_write
from app.db.repository import (
    AsioLastContactRepository,
    AvAssignmentCacheRepository,
    BreakdownCacheRepository,
    CompanyAliasRepository,
    CompanyMappingRepository,
    DeviceAvStatusRepository,
    PlatformDeviceRepository,
    ReconciledDeviceRepository,
    ReconciliationRepository,
    get_session_factory,
)
from app.db.schema import ReconciledDevice
from app.services.company_matcher import build_cross_platform_aliases, build_device_cross_reference_aliases
from app.services.device_identity import (
    detect_endpoint_changes,
    prepare_company_mappings,
    prepare_device_rows,
    reconcile_from_db,
)
from app.services.normalizers import (
    merge_prefix_duplicates,
    normalize_company_name,
    normalize_hostname,
    normalize_mac,
    normalize_serial,
)
from app.services.reconciler import reconcile_devices
from app.services.confirmation import build_preview, enforce_confirmation

log = structlog.get_logger("companies")

router = APIRouter(prefix="/api/companies", tags=["companies"])

# Cache for reconciled data: (timestamp, data)
_reconciled_cache: dict[str, Any] = {"ts": None, "data": None, "av_map": None}
_CACHE_TTL_SECONDS = 900  # 15 minutes


def invalidate_reconciled_cache() -> None:
    """Clear the reconciled-data cache so the next GET rebuilds from DB.

    Called by the reconciliation service after a fast/smart run completes
    to ensure the Breakdown tab reflects the freshly-computed data.
    """
    _reconciled_cache["ts"] = None
    _reconciled_cache["data"] = None
    _reconciled_cache["av_map"] = None


def _get_psa_connector():
    """Return the ConnectWise PSA connector instance, or None."""
    connectors = get_connectors()
    for c in connectors:
        if c.platform_name == "connectwise_psa":
            return c
    return None


def _sanitize_for_pg(val: str) -> str:
    """Strip null bytes that PostgreSQL rejects (\x00)."""
    return val.replace("\x00", "") if val else val


class AsioLastContactUploadRequest(BaseModel):
    """Request body for importing Asio last-contact timestamps from CSV text."""

    filename: str = "asio-last-contact.csv"
    content: str


def _csv_header_key(value: str) -> str:
    """Normalise CSV header names for flexible matching."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _csv_value(row: dict[str, str], aliases: tuple[str, ...]) -> str:
    """Return the first non-empty value matching any normalised header alias."""
    alias_set = {_csv_header_key(alias) for alias in aliases}
    for key, value in row.items():
        if _csv_header_key(key) in alias_set and value and value.strip():
            return _sanitize_for_pg(value.strip().lstrip("\ufeff"))
    return ""


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a timestamp from API/CSV data for export formatting."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value <= 1_000_000_000:
            return None
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    raw = str(value).strip()
    if not raw or raw.lower() in {"never", "n/a", "null", "none", "0"}:
        return None
    if raw.isdigit():
        return _parse_timestamp(int(raw))
    match = re.search(r"/Date\((\d+)\)/", raw)
    if match:
        return _parse_timestamp(int(match.group(1)))

    iso_value = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass

    for fmt in (
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M",
        "%m/%d/%y %I:%M %p",
        "%m/%d/%y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
    ):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _format_offline_age(value: Any) -> str:
    """Format a timestamp as relative offline age, matching the UI wording."""
    parsed = _parse_timestamp(value)
    if not parsed:
        return ""
    now = datetime.now(UTC)
    if parsed > now:
        return "recent"
    diff = now - parsed
    days = diff.days
    hours = int(diff.total_seconds() // 3600)
    if days >= 1:
        return f"{days}d ago"
    if hours >= 1:
        return f"{hours}h ago"
    return "recent"


def _offline_age_days(value: Any) -> int | None:
    """Return whole offline days for sorting/styling, or None when unknown."""
    parsed = _parse_timestamp(value)
    if not parsed:
        return None
    now = datetime.now(UTC)
    if parsed > now:
        return 0
    return (now - parsed).days


def _sorted_sc_asio_export(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return SC-vs-Asio export rows sorted alphabetically by company and hostname."""
    sorted_companies = sorted(data, key=lambda c: (c.get("company") or "").lower())
    for company in sorted_companies:
        company["missing_from_asio"] = sorted(
            company.get("missing_from_asio", []),
            key=lambda d: ((d.get("hostname") or "").lower(), (d.get("site_name") or "").lower()),
        )
        company["missing_from_sc"] = sorted(
            company.get("missing_from_sc", []),
            key=lambda d: ((d.get("hostname") or "").lower(), (d.get("site_name") or "").lower()),
        )
    return sorted_companies


def _compute_endpoint_protection(dev: dict[str, Any], assigned_av: str) -> str:
    """Compute the endpoint_protection label for a reconciled device.

    Each company uses ONE AV — either ESET *or* SentinelOne, never both.
    ``assigned_av`` comes from PSA agreements ("eset", "sentinelone", or
    "none").  The label reflects the per-device detected AV with version,
    e.g. ``"SentinelOne 25.1.4.434"`` or ``"ESET 12.1.2076.1"``.
    """
    eset_ver = dev.get("eset_version", "")
    s1_ver = dev.get("s1_agent_version", "")
    dev_has_s1 = "sentinelone" in dev.get("platforms", [])
    dev_has_eset = dev.get("eset_installed") or "eset" in dev.get("av_vendors", [])

    # Only show the AV that matches the company assignment.
    is_s1_company = assigned_av == "sentinelone"
    is_eset_company = assigned_av == "eset"

    # Per-device detection — show only the assigned AV *actually found*.
    # If the assigned AV is NOT detected on the device, do NOT claim it is
    # protected: report "None (<AV> expected)" so the label reflects reality
    # (the contractual AV is still named for context). This keeps the AV
    # compliance / "missing AV" report internally consistent.
    if is_s1_company:
        if dev_has_s1 and s1_ver:
            return f"SentinelOne {s1_ver}"
        if dev_has_s1:
            return "SentinelOne"
        return "None (SentinelOne expected)"
    if is_eset_company:
        if dev_has_eset and eset_ver:
            return f"ESET {eset_ver}"
        if dev_has_eset:
            return "ESET"
        return "None (ESET expected)"

    # No PSA assignment — fall back to per-device detection (prefer versioned)
    if dev_has_s1 and s1_ver:
        return f"SentinelOne {s1_ver}"
    if dev_has_eset and eset_ver:
        return f"ESET {eset_ver}"
    if dev_has_s1:
        return "SentinelOne"
    if dev_has_eset:
        return "ESET"
    return "None"


# Maps the requested AV vendor to the set of ``assigned_av`` values that mean
# "this company is contracted for that AV" (``both`` counts for either vendor).
_AV_ASSIGNED_VALUES: dict[str, set[str]] = {
    "eset": {"eset", "both"},
    "sentinelone": {"sentinelone", "both"},
}


def _device_has_av(dev: dict[str, Any], vendor: str) -> bool:
    """Return True when *dev* already has the requested AV installed.

    For ESET this checks the per-device ``eset_installed`` flag (and the
    detected ``av_vendors`` list as a fallback).  For SentinelOne the S1 agent
    *is* the AV, so presence of the ``sentinelone`` platform counts as
    installed.
    """
    if vendor == "sentinelone":
        return "sentinelone" in dev.get("platforms", [])
    # Default: ESET
    return bool(dev.get("eset_installed")) or "eset" in dev.get("av_vendors", [])


def _other_av_detected(dev: dict[str, Any], vendor: str) -> str:
    """Return any AV detected on *dev* that is NOT the expected *vendor*.

    Used so a "missing ESET" row can explain *why* it is flagged: either the
    machine has no AV at all, or it has a different AV (e.g. SentinelOne)
    installed instead.  Returns a comma-joined label or ``""`` when none.
    """
    found: list[str] = []
    if vendor != "sentinelone" and "sentinelone" in dev.get("platforms", []):
        found.append("SentinelOne")
    for av in dev.get("av_vendors", []):
        if av and av != vendor:
            found.append(av if av == "eset" else av.title())
    # De-dupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for f in found:
        key = f.lower()
        if key not in seen:
            seen.add(key)
            out.append(f)
    return ", ".join(out)


def _av_gap_reason(dev: dict[str, Any], vendor: str, other_av: str) -> str:
    """Classify why a device is flagged as missing the expected AV.

    This is the key evidence for support documentation — it distinguishes a
    machine with **no endpoint protection** from one that simply has a
    **different AV** than the company is contracted for, or one whose AV record
    failed to match into the AV inventory/portal.
    """
    if other_av:
        return f"Different AV detected ({other_av}), not {vendor.upper()}"
    return f"No {vendor.upper()} detected (no AV evidence in Asio scan or portal)"



def _build_av_compliance(
    reconciled: list[dict[str, Any]], vendor: str,
) -> dict[str, Any]:
    """Project reconciled data into an AV-compliance report for one vendor.

    Returns companies whose PSA agreement assigns the requested AV (``vendor``
    is ``"eset"`` or ``"sentinelone"``) and, for each, the Asio machines that
    are **missing** that AV.  This is a pure read-only projection over the
    already-reconciled inventory — no external API calls are made.

    A machine is "missing" the AV when it has an Asio agent
    (``"asio" in platforms``) but the AV is not detected on it.

    Returns::

        {
            "vendor": "eset",
            "generated_at": "...",
            "totals": {"companies": N, "asio_devices": N,
                       "compliant": N, "missing": N},
            "companies": [
                {"company": "...", "assigned_av": "eset",
                 "asio_total": N, "compliant": N, "missing": N,
                 "missing_devices": [{"hostname", "endpoint_id",
                                      "site_name", "platforms",
                                      "endpoint_protection"}, ...]},
                ...
            ],
        }
    """
    vendor = (vendor or "").lower().strip()
    assigned_values = _AV_ASSIGNED_VALUES.get(vendor)
    if assigned_values is None:
        raise ValueError(
            f"Unsupported vendor '{vendor}'. Expected 'eset' or 'sentinelone'."
        )

    companies_out: list[dict[str, Any]] = []
    tot_asio = tot_compliant = tot_missing = 0
    tot_no_av = tot_other_av = 0

    for company_data in reconciled:
        assigned = company_data.get("assigned_av", "none")
        if assigned not in assigned_values:
            continue

        company_name = company_data.get("company", "")
        asio_total = 0
        compliant = 0
        missing_devices: list[dict[str, Any]] = []
        no_av_count = 0
        other_av_count = 0

        # Walk both matched devices and unmatched (per-platform) devices so a
        # machine that only exists on Asio is still evaluated.
        all_devices: list[dict[str, Any]] = list(company_data.get("matched", []))
        for _plat, devs in company_data.get("mismatched", {}).items():
            all_devices.extend(devs)

        for dev in all_devices:
            if "asio" not in dev.get("platforms", []):
                continue
            asio_total += 1
            if _device_has_av(dev, vendor):
                compliant += 1
            else:
                other_av = _other_av_detected(dev, vendor)
                if other_av:
                    other_av_count += 1
                else:
                    no_av_count += 1
                missing_devices.append({
                    "hostname": dev.get("hostname", ""),
                    "endpoint_id": dev.get("endpoint_id", ""),
                    "site_name": dev.get("site_name", ""),
                    "platforms": dev.get("platforms", []),
                    # Honest detection label (e.g. "None (ESET expected)").
                    "endpoint_protection": dev.get("endpoint_protection", ""),
                    # Evidence for support: what (if anything) WAS detected and
                    # the specific reason this machine is flagged.
                    "detected_av": other_av or "None",
                    "gap_reason": _av_gap_reason(dev, vendor, other_av),
                    "eset_installed": bool(dev.get("eset_installed")),
                    "av_vendors": dev.get("av_vendors", []),
                    "match_type": dev.get("match_type", ""),
                })

        # Sort missing machines by hostname for stable, readable output.
        missing_devices.sort(key=lambda d: (d.get("hostname") or "").lower())

        companies_out.append({
            "company": company_name,
            "assigned_av": assigned,
            "asio_total": asio_total,
            "compliant": compliant,
            "missing": len(missing_devices),
            # Split the gap so support can tell genuine "no protection" machines
            # apart from machines running a different/unmatched AV.
            "missing_no_av": no_av_count,
            "missing_other_av": other_av_count,
            "summary": (
                f"{len(missing_devices)} of {asio_total} Asio device(s) "
                f"missing {vendor.upper()} "
                f"({no_av_count} with no AV detected, "
                f"{other_av_count} with a different AV detected)"
            ),
            "missing_devices": missing_devices,
        })

        tot_asio += asio_total
        tot_compliant += compliant
        tot_missing += len(missing_devices)
        tot_no_av += no_av_count
        tot_other_av += other_av_count

    # Companies with the most missing machines first — that's where the
    # failing-install remediation effort should focus.
    companies_out.sort(key=lambda c: (-c["missing"], c["company"].lower()))

    return {
        "vendor": vendor,
        "generated_at": datetime.now(UTC).isoformat(),
        "totals": {
            "companies": len(companies_out),
            "asio_devices": tot_asio,
            "compliant": tot_compliant,
            "missing": tot_missing,
            "missing_no_av": tot_no_av,
            "missing_other_av": tot_other_av,
        },
        "companies": companies_out,
    }


async def _build_eset_connect_lookups() -> tuple[
    dict[str, dict[str, str]],
    dict[str, dict[str, str]],
    dict[str, dict[str, str]],
]:
    """Fetch ESET Connect (Protect Cloud) devices and build lookup maps.

    Returns ``(by_hostname, by_mac, by_serial)`` dicts mapping normalised
    identifiers to ``{"eset_product": ..., "eset_version": ...}``.
    """
    from app.connectors.eset_connect import EsetConnectConnector

    connector = EsetConnectConnector()
    if connector.check_config():
        return {}, {}, {}

    try:
        if not await connector.authenticate():
            return {}, {}, {}
        devices = await connector.get_devices()
    except Exception:  # noqa: BLE001
        return {}, {}, {}

    by_hostname: dict[str, dict[str, str]] = {}
    by_mac: dict[str, dict[str, str]] = {}
    by_serial: dict[str, dict[str, str]] = {}

    for device in devices:
        display_name = device.get("displayName", "")
        hn = normalize_hostname(display_name) if display_name else ""

        components = EsetConnectConnector.extract_eset_versions(device)
        eset_product = ""
        eset_version = ""
        for comp in components:
            name_lower = comp["product"].lower()
            if "eset" in name_lower and ("endpoint" in name_lower or "security" in name_lower):
                eset_product = comp["product"]
                eset_version = comp["version"]
                break
        if not eset_product and components:
            eset_product = components[0]["product"]
            eset_version = components[0]["version"]
        if not eset_product:
            active = EsetConnectConnector.extract_active_products(device)
            if active:
                eset_product = active[0].get("name", "ESET (managed)")
            else:
                eset_product = "ESET (managed)"

        entry = {"eset_product": eset_product, "eset_version": eset_version}

        if hn:
            by_hostname[hn] = entry

        for hw_profile in (device.get("hardwareProfiles") or []):
            hw = hw_profile if isinstance(hw_profile, dict) else {}
            for adapter in hw.get("networkAdapters", []):
                mac_raw = adapter.get("macAddress", "")
                mac_norm = normalize_mac(mac_raw)
                if mac_norm:
                    by_mac[mac_norm] = entry
            bios = hw.get("bios") or {}
            serial_raw = bios.get("serialNumber", "")
            serial_norm = normalize_serial(serial_raw)
            if serial_norm:
                by_serial[serial_norm] = entry

    return by_hostname, by_mac, by_serial


def _enrich_device_with_eset_connect(
    dev: dict[str, Any],
    by_hostname: dict[str, dict[str, str]],
    by_mac: dict[str, dict[str, str]],
    by_serial: dict[str, dict[str, str]],
) -> bool:
    """Match a reconciled device against ESET Connect data and enrich it.

    Checks hostname, MAC address(es), then serial number in priority order.
    Returns ``True`` if the device matched an ESET Connect record.
    """
    hn = normalize_hostname(dev.get("hostname", ""))
    serial = normalize_serial(dev.get("serial_number", ""))

    match = None
    if hn and hn in by_hostname:
        match = by_hostname[hn]

    if not match:
        # MAC field may contain multiple addresses separated by ", "
        raw_mac = dev.get("mac_address", "")
        for mac_part in raw_mac.replace(",", " ").split():
            mac_norm = normalize_mac(mac_part.strip())
            if mac_norm and mac_norm in by_mac:
                match = by_mac[mac_norm]
                break

    if not match and serial and serial in by_serial:
        match = by_serial[serial]

    if match:
        dev["eset_installed"] = True
        dev["eset_product"] = match["eset_product"]
        if match["eset_version"]:
            dev["eset_version"] = match["eset_version"]
        av_vendors: list[str] = dev.get("av_vendors", [])
        if "eset" not in av_vendors:
            dev["av_vendors"] = sorted(set(av_vendors) | {"eset"})
        return True
    return False


async def _get_session() -> AsyncSession:  # type: ignore[return]
    """Dependency that yields a database session."""
    factory = get_session_factory()
    async with factory() as session:
        yield session  # type: ignore[misc]


@router.get("", response_model=list[dict[str, Any]])
async def list_companies(session: AsyncSession = Depends(_get_session)) -> list[dict[str, Any]]:
    """Return all stored company match results."""
    repo = ReconciliationRepository(session)
    matches = await repo.list_company_matches()
    return [
        {
            "id": m.id,
            "platform_a": m.platform_a,
            "platform_id_a": m.platform_id_a,
            "name_a": m.name_a,
            "platform_b": m.platform_b,
            "platform_id_b": m.platform_id_b,
            "name_b": m.name_b,
            "score": m.score,
            "status": m.status,
        }
        for m in matches
    ]


@router.get("/summary", response_model=list[dict[str, Any]])
async def company_summary(
    session: AsyncSession = Depends(_get_session),
) -> list[dict[str, Any]]:
    """Return per-company device counts broken down by platform.

    Fetches live data from all active connectors, normalises company names,
    applies alias mappings (merges), and returns one row per unique company
    with device counts per platform.
    """
    connectors = get_connectors()
    if not connectors:
        return []

    # Load alias map from DB
    alias_repo = CompanyAliasRepository(session)
    alias_map = await alias_repo.get_alias_map()

    # Fetch companies and devices from all connectors concurrently
    async def _safe_fetch(connector):  # type: ignore[no-untyped-def]
        try:
            companies = await connector.get_companies()
            devices = await connector.get_devices()
            return connector.platform_name, companies, devices
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "company_summary.connector_error",
                platform=connector.platform_name,
                error=str(exc),
            )
            return connector.platform_name, [], []

    results = await asyncio.gather(*[_safe_fetch(c) for c in connectors])

    # Build cross-platform auto-aliases from company records
    companies_by_platform: dict[str, list] = {}
    for platform, companies, _devices in results:
        companies_by_platform.setdefault(platform, []).extend(companies)
    auto_aliases = await asyncio.to_thread(build_cross_platform_aliases,companies_by_platform, alias_map)
    device_aliases = await asyncio.to_thread(build_device_cross_reference_aliases,
        [(platform, devices) for platform, _companies, devices in results], alias_map
    )
    merged_alias_map = {**auto_aliases, **device_aliases, **alias_map}

    # Build: normalised_company_name → { platform → device_count }
    company_platforms: dict[str, dict[str, int]] = {}
    all_platforms: set[str] = set()

    # Track company-level presence per platform (from get_companies())
    # This is important for platforms like ESET that have companies but no devices
    company_presence: dict[str, set[str]] = {}  # norm_name → set of platforms
    for platform, companies, _devices in results:
        all_platforms.add(platform)
        for comp in companies:
            if comp.name:
                canon = merged_alias_map.get(comp.name, comp.name)
                company_presence.setdefault(canon, set()).add(platform)

    for platform, _companies, devices in results:
        for device in devices:
            # Determine company name from device
            company = device.company_name or device.company_platform_id or ""
            if not company:
                continue
            norm = normalize_company_name(company) if company else ""
            if not norm:
                continue
            # Apply alias mapping — resolve to canonical name
            norm = merged_alias_map.get(norm, norm)
            company_platforms.setdefault(norm, {})
            company_platforms[norm].setdefault(platform, 0)
            company_platforms[norm][platform] += 1

    # Merge companies that share the same first 3 words (API truncation dedup)
    company_platforms = merge_prefix_duplicates(company_platforms)

    # Also apply prefix dedup to company_presence keys
    presence_deduped: dict[str, set[str]] = {}
    for comp, plats in company_presence.items():
        prefix = " ".join(comp.split()[:3])
        # Find canonical key from merged company_platforms
        matched = False
        for canon in company_platforms:
            if " ".join(canon.split()[:3]) == prefix:
                presence_deduped.setdefault(canon, set()).update(plats)
                matched = True
                break
        if not matched:
            presence_deduped.setdefault(comp, set()).update(plats)

    # Fetch per-company AV assignment from PSA agreement additions (best-effort)
    av_map: dict[str, str] = {}
    psa = _get_psa_connector()
    if psa:
        try:
            raw_av = await psa.get_company_av_map()
            # Build prefix canonical for dedup
            merged_words_summary: dict[str, str] = {}
            for canon in company_platforms:
                pfx = " ".join(canon.split()[:3])
                merged_words_summary[pfx] = canon
            for name, av in raw_av.items():
                # Try direct match first, then prefix match
                if name in company_platforms:
                    av_map[name] = av
                else:
                    pfx = " ".join(name.split()[:3])
                    if pfx in merged_words_summary:
                        av_map[merged_words_summary[pfx]] = av
        except Exception:  # noqa: BLE001
            pass

    # Build response rows sorted by company name
    sorted_platforms = sorted(all_platforms)
    rows: list[dict[str, Any]] = []
    for company_name in sorted(company_platforms.keys()):
        platform_counts = company_platforms[company_name]
        total = sum(platform_counts.values())
        # Add company presence info for device-less platforms (e.g. ESET)
        presence = presence_deduped.get(company_name, set())
        # Derive AV status from PSA assignment — one AV per company
        assigned = av_map.get(company_name, "none")
        if assigned == "sentinelone":
            av_status = "SentinelOne"
        elif assigned == "eset":
            av_status = "ESET"
        else:
            # No PSA assignment — fall back to device detection
            eset_here = "eset" in presence
            s1_here = platform_counts.get("sentinelone", 0) > 0
            if s1_here:
                av_status = "SentinelOne"
            elif eset_here:
                av_status = "ESET"
            else:
                av_status = "None"
        rows.append({
            "company": company_name,
            "total_devices": total,
            "platforms": {p: platform_counts.get(p, 0) for p in sorted_platforms},
            "company_presence": {p: (p in presence) for p in sorted_platforms},
            "platform_list": sorted_platforms,
            "assigned_av": assigned,
            "av_status": av_status,
        })
    return rows


# ── Per-Platform Company Names ────────────────────────────────────


@router.get("/by-platform", response_model=list[dict[str, Any]])
async def companies_by_platform(
    session: AsyncSession = Depends(_get_session),
) -> list[dict[str, Any]]:
    """Return every company name reported by each platform.

    For each platform the response includes:
    - ``platform``: connector name
    - ``companies``: list of normalised company names from ``get_companies()``
    - ``device_companies``: list of normalised company names seen in devices
    - ``company_count``: how many companies the platform reports
    - ``device_company_count``: how many unique company names appear in devices

    Uses auto-aliases (fuzzy matching + device hostname cross-reference)
    in addition to manual DB aliases for accurate cross-platform matching.
    PSA companies are filtered to managed-services / SentinelOne agreement holders.
    """
    connectors = get_connectors()
    if not connectors:
        return []

    # Load alias map from DB
    alias_repo = CompanyAliasRepository(session)
    alias_map = await alias_repo.get_alias_map()

    async def _safe_fetch(connector):  # type: ignore[no-untyped-def]
        try:
            # For PSA, use only managed companies (not every company in the PSA)
            if hasattr(connector, "get_managed_companies"):
                companies = await connector.get_managed_companies()
            else:
                companies = await connector.get_companies()
            devices = await connector.get_devices()
            return connector.platform_name, companies, devices
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "by_platform.connector_error",
                platform=connector.platform_name,
                error=str(exc),
            )
            return connector.platform_name, [], []

    results = await asyncio.gather(*[_safe_fetch(c) for c in connectors])

    # Build auto-aliases (same as summary endpoint) for accurate matching
    companies_by_plat: dict[str, list] = {}
    for platform, companies, _devices in results:
        companies_by_plat.setdefault(platform, []).extend(companies)
    auto_aliases = await asyncio.to_thread(build_cross_platform_aliases,companies_by_plat, alias_map)
    device_aliases = await asyncio.to_thread(build_device_cross_reference_aliases,
        [(platform, devices) for platform, _companies, devices in results], alias_map
    )
    merged_alias_map = {**device_aliases, **auto_aliases, **alias_map}

    # Build a set of all canonical company names across all platforms
    all_canonical: dict[str, set[str]] = {}  # canonical → set of platforms

    rows: list[dict[str, Any]] = []
    platform_data: dict[str, dict[str, Any]] = {}

    for platform, companies, devices in results:
        # Company names from get_companies()
        company_names: list[str] = sorted({
            c.name for c in companies if c.name
        })
        # Company names derived from device records
        device_company_names: list[str] = sorted({
            normalize_company_name(d.company_name)
            for d in devices
            if d.company_name and normalize_company_name(d.company_name)
        })

        platform_data[platform] = {
            "company_names": company_names,
            "device_company_names": device_company_names,
        }

        # Track which platforms each canonical name appears on
        for name in set(company_names) | set(device_company_names):
            canon = merged_alias_map.get(name, name)
            all_canonical.setdefault(canon, set()).add(platform)

    # Build response with match info
    for platform in sorted(platform_data.keys()):
        data = platform_data[platform]
        company_names = data["company_names"]
        device_company_names = data["device_company_names"]
        all_names = sorted(set(company_names) | set(device_company_names))

        # Annotate each name with alias info and cross-platform presence
        annotated: list[dict[str, Any]] = []
        for name in all_names:
            canon = merged_alias_map.get(name, name)
            other_platforms = sorted(all_canonical.get(canon, set()) - {platform})
            annotated.append({
                "name": name,
                "canonical": canon if canon != name else None,
                "has_alias": name in merged_alias_map,
                "in_companies": name in company_names,
                "in_devices": name in device_company_names,
                "also_on": other_platforms,
            })

        rows.append({
            "platform": platform,
            "companies": annotated,
            "company_count": len(company_names),
            "device_company_count": len(device_company_names),
            "total_unique": len(all_names),
        })

    return rows


# ── Company Alias (Merge) Endpoints ───────────────────────────────


class CompanyMergeRequest(BaseModel):
    """Request body for merging two company names."""

    source_name: str
    target_name: str
    note: str | None = None


@router.post("/merge", response_model=dict[str, Any], status_code=201)
async def merge_companies(
    body: CompanyMergeRequest,
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Create a company alias so *source_name* merges into *target_name*.

    Both names are normalised before storage.  On subsequent loads the
    source company's devices will appear under the target company row.
    """
    alias_name = normalize_company_name(body.source_name)
    canonical_name = normalize_company_name(body.target_name)
    if not alias_name or not canonical_name:
        raise HTTPException(status_code=400, detail="Both source and target names are required.")
    if alias_name == canonical_name:
        raise HTTPException(status_code=400, detail="Source and target names are identical after normalisation.")

    repo = CompanyAliasRepository(session)
    obj = await repo.create_alias(alias_name, canonical_name, body.note)
    return {
        "id": obj.id,
        "alias_name": obj.alias_name,
        "canonical_name": obj.canonical_name,
        "note": obj.note,
    }


@router.get("/aliases", response_model=list[dict[str, Any]])
async def list_aliases(
    session: AsyncSession = Depends(_get_session),
) -> list[dict[str, Any]]:
    """Return all company aliases."""
    repo = CompanyAliasRepository(session)
    aliases = await repo.list_aliases()
    return [
        {
            "id": a.id,
            "alias_name": a.alias_name,
            "canonical_name": a.canonical_name,
            "note": a.note,
        }
        for a in aliases
    ]


@router.delete("/aliases/{alias_id}", response_model=dict[str, Any])
async def delete_alias(
    alias_id: str,
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Delete a company alias by ID."""
    repo = CompanyAliasRepository(session)
    removed = await repo.delete_alias(alias_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Alias not found.")
    return {"deleted": True, "id": alias_id}


# ── Per-Company AV Assignment (from PSA Agreement Additions) ──────


@router.get("/av-map", response_model=dict[str, str])
async def company_av_map() -> dict[str, str]:
    """Return normalised company name → assigned AV product from PSA additions.

    Values: ``"eset"``, ``"sentinelone"``, ``"both"``, or ``"none"``.
    """
    psa = _get_psa_connector()
    if not psa:
        return {}
    return await psa.get_company_av_map()


# ── Scan: Persist Devices & Company Mappings to Database ──────────


@router.post("/scan", response_model=dict[str, Any])
async def scan_and_persist(
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Pull all connectors, persist devices + company mappings to DB, detect changes.

    This is the primary "refresh" action.  It:

    1. Fetches companies + devices from every active connector concurrently.
    2. Builds the alias map (DB aliases + fuzzy auto-aliases + device cross-ref).
    3. Converts device records into DB-ready rows via ``prepare_device_rows()``.
    4. Persists rows via ``PlatformDeviceRepository.bulk_upsert()`` and
       ``CompanyMappingRepository.bulk_upsert()``.
    5. Detects endpoint-ID changes (added / removed / company-moved) per
       platform that carries endpoint IDs.
    6. Marks stale devices (no longer in scan) for removal.
    7. Invalidates the reconciled cache so the next GET reads fresh DB data.
    8. Returns a change summary.
    """
    import structlog as _structlog

    _log = _structlog.get_logger("companies.scan")

    connectors = get_connectors()
    if not connectors:
        return {"status": "no_connectors", "platforms": {}}

    # Load alias map from DB
    alias_repo = CompanyAliasRepository(session)
    alias_map = await alias_repo.get_alias_map()

    # Fetch companies + devices concurrently
    connector_errors: dict[str, str] = {}

    async def _safe_fetch(connector):  # type: ignore[no-untyped-def]
        try:
            companies = await connector.get_companies()
            devices = await connector.get_devices()
            return connector.platform_name, companies, devices
        except Exception as exc:  # noqa: BLE001
            error_msg = str(exc)
            _log.warning("scan.connector_error", platform=connector.platform_name, error=error_msg)
            connector_errors[connector.platform_name] = error_msg
            return connector.platform_name, [], []

    results = await asyncio.gather(*[_safe_fetch(c) for c in connectors])

    # Build cross-platform auto-aliases
    companies_by_platform: dict[str, list] = {}
    platform_devices_raw: list[tuple[str, list]] = []
    for platform, companies, devices in results:
        companies_by_platform.setdefault(platform, []).extend(companies)
        platform_devices_raw.append((platform, devices))

    auto_aliases = await asyncio.to_thread(build_cross_platform_aliases,companies_by_platform, alias_map)
    device_aliases = await asyncio.to_thread(build_device_cross_reference_aliases,platform_devices_raw, alias_map)
    merged_alias_map = {**device_aliases, **auto_aliases, **alias_map}

    # Prepare DB-ready rows
    device_rows = prepare_device_rows(platform_devices_raw, merged_alias_map)
    company_mappings = prepare_company_mappings(platform_devices_raw, merged_alias_map)

    # Persist
    device_repo = PlatformDeviceRepository(session)
    mapping_repo = CompanyMappingRepository(session)

    # Detect changes BEFORE upserting (compare incoming vs stored)
    change_summary: dict[str, Any] = {}
    for platform, _companies, devices in results:
        # Build incoming endpoint_id map for this platform
        incoming_eid: dict[str, str] = {}
        for row in device_rows:
            if row["platform"] == platform and row["endpoint_id"]:
                incoming_eid[row["endpoint_id"]] = row["company_canonical"]
        if incoming_eid:
            stored_eid = await device_repo.get_stored_endpoint_ids(platform)
            changes = detect_endpoint_changes(incoming_eid, stored_eid)
            change_summary[platform] = changes

    # Upsert devices + mappings
    upsert_stats = await device_repo.bulk_upsert(device_rows)
    mapping_count = await mapping_repo.bulk_upsert(company_mappings)

    # Mark stale devices per platform
    stale_counts: dict[str, int] = {}
    for platform, _companies, devices in results:
        if devices:
            active_ids = {row["platform_id"] for row in device_rows if row["platform"] == platform}
            stale = await device_repo.mark_stale(platform, active_ids)
            if stale:
                stale_counts[platform] = stale

    # Invalidate reconciled cache
    _reconciled_cache["ts"] = None
    _reconciled_cache["data"] = None
    _reconciled_cache["av_map"] = None

    per_platform: dict[str, dict[str, Any]] = {}
    for platform, companies, devices in results:
        entry: dict[str, Any] = {
            "companies_fetched": len(companies),
            "devices_fetched": len(devices),
            "stale_removed": stale_counts.get(platform, 0),
        }
        if platform in connector_errors:
            entry["status"] = "error"
            entry["error"] = connector_errors[platform]
        else:
            entry["status"] = "ok"
        if platform in change_summary:
            cs = change_summary[platform]
            entry["endpoint_changes"] = {
                "added": len(cs["added"]),
                "removed": len(cs["removed"]),
                "moved": len(cs["moved"]),
                "unchanged": cs["unchanged"],
            }
            if cs["moved"]:
                entry["endpoint_changes"]["moved_details"] = cs["moved"]
        per_platform[platform] = entry

    _log.info(
        "scan.complete",
        inserted=upsert_stats["inserted"],
        updated=upsert_stats["updated"],
        mappings=mapping_count,
    )

    result: dict[str, Any] = {
        "status": "ok" if not connector_errors else "partial",
        "devices_inserted": upsert_stats["inserted"],
        "devices_updated": upsert_stats["updated"],
        "company_mappings": mapping_count,
        "platforms": per_platform,
    }
    if connector_errors:
        result["connector_errors"] = connector_errors
    return result


# ── Hierarchical Company → Platform → Site → Device Breakdown ─────


@router.get(
    "/breakdown",
    response_model=list[dict[str, Any]],
    summary="Company → platform → site → device breakdown with matched/unmatched counts",
)
async def company_breakdown(
    session: AsyncSession = Depends(_get_session),
) -> list[dict[str, Any]]:
    """Return a hierarchical Company → Platform → Site → Device breakdown.

    For each company (after alias + prefix dedup) the response includes
    per-platform entries, each broken down by site with individual device
    hostnames.  This enables the frontend to render an expandable tree
    view: Company → Platform → Site → Computer.
    """
    connectors = get_connectors()
    if not connectors:
        return []

    # Load alias map from DB
    alias_repo = CompanyAliasRepository(session)
    alias_map = await alias_repo.get_alias_map()

    # Fetch devices from all connectors concurrently
    async def _safe_devices(connector):  # type: ignore[no-untyped-def]
        try:
            devices = await connector.get_devices()
            return connector.platform_name, devices
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "breakdown.device_fetch_error",
                platform=connector.platform_name,
                error=str(exc),
            )
            return connector.platform_name, []

    async def _safe_companies(connector):  # type: ignore[no-untyped-def]
        try:
            companies = await connector.get_companies()
            return connector.platform_name, companies
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "breakdown.company_fetch_error",
                platform=connector.platform_name,
                error=str(exc),
            )
            return connector.platform_name, []

    results = await asyncio.gather(*[_safe_devices(c) for c in connectors])

    # Fetch company records from all connectors for cross-platform matching
    company_results = await asyncio.gather(*[_safe_companies(c) for c in connectors])
    companies_by_platform: dict[str, list] = {}
    for plat, companies in company_results:
        companies_by_platform.setdefault(plat, []).extend(companies)
    auto_aliases = await asyncio.to_thread(build_cross_platform_aliases,companies_by_platform, alias_map)
    device_aliases = await asyncio.to_thread(build_device_cross_reference_aliases,list(results), alias_map)
    merged_alias_map = {**auto_aliases, **device_aliases, **alias_map}

    # Collect all canonical company names for prefix dedup
    # key = normalised company name, value = { platform → { site → [devices] } }
    raw_tree: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )

    for platform, devices in results:
        for dev in devices:
            company = dev.company_name or dev.company_platform_id or ""
            if not company:
                continue
            norm = normalize_company_name(company)
            if not norm:
                continue
            norm = merged_alias_map.get(norm, norm)
            site = dev.site_name or "(no site)"
            raw_tree[norm][platform][site].append({
                "hostname": dev.hostname,
                "platform_id": dev.platform_id,
                "mac_addresses": dev.mac_addresses,
                "serial_number": dev.serial_number,
                "site_id": dev.site_id,
                "endpoint_id": dev.endpoint_id,
                "company_platform_id": dev.company_platform_id,
            })

    # Prefix dedup: merge companies sharing first 3 words
    # Build a simple count map so merge_prefix_duplicates can work
    count_map: dict[str, dict[str, int]] = {}
    for comp in raw_tree:
        count_map[comp] = {"_total": 1}
    merged_count = merge_prefix_duplicates(count_map)

    # Build canonical name mapping from dedup
    # For every company in raw_tree, find which merged key it falls under
    canonical_map: dict[str, str] = {}
    merged_words: dict[str, str] = {}
    for canon in merged_count:
        words = canon.split()[:3]
        prefix = " ".join(words)
        merged_words[prefix] = canon
    for comp in raw_tree:
        words = comp.split()[:3]
        prefix = " ".join(words)
        canonical_map[comp] = merged_words.get(prefix, comp)

    # Rebuild tree with merged company names
    merged_tree: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for comp, platforms in raw_tree.items():
        canon = canonical_map.get(comp, comp)
        for platform, sites in platforms.items():
            for site, devices in sites.items():
                merged_tree[canon][platform][site].extend(devices)

    # Build response
    rows: list[dict[str, Any]] = []
    for company_name in sorted(merged_tree.keys()):
        platforms_data: list[dict[str, Any]] = []
        total_devices = 0
        for platform in sorted(merged_tree[company_name].keys()):
            sites_data: list[dict[str, Any]] = []
            platform_count = 0
            for site_name in sorted(merged_tree[company_name][platform].keys()):
                devs = merged_tree[company_name][platform][site_name]
                sites_data.append({
                    "site_name": site_name,
                    "device_count": len(devs),
                    "devices": sorted(devs, key=lambda d: d.get("hostname", "")),
                })
                platform_count += len(devs)
            platforms_data.append({
                "platform": platform,
                "device_count": platform_count,
                "sites": sites_data,
            })
            total_devices += platform_count
        rows.append({
            "company": company_name,
            "total_devices": total_devices,
            "platforms": platforms_data,
        })
    return rows


# ── Reconciled Devices — Cross-Platform Matched View ──────────────


@router.post("/asio-last-contact/upload", response_model=dict[str, Any])
async def upload_asio_last_contact_csv(
    body: AsioLastContactUploadRequest,
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Import Asio last-contact timestamps from a user-provided CSV.

    Accepted columns are flexible. The CSV must include a last-contact style
    column plus either an endpoint/device ID or a hostname. Examples:
    ``Endpoint ID``, ``Asio Device ID``, ``Name``, ``Friendly Name``,
    ``Device Name``, ``Last Contact``, ``Last Online``, or ``Last Seen``.
    """
    content = body.content or ""
    if not content.strip():
        raise HTTPException(status_code=400, detail="CSV content is empty.")
    if len(content.encode("utf-8")) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="CSV is too large. Maximum size is 5 MB.")

    sample = content[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV header row was not found.")

    rows: list[dict[str, str]] = []
    skipped = 0
    errors: list[str] = []

    endpoint_aliases = (
        "endpoint_id", "endpoint id", "endpointid", "asio device id", "asio deviceid",
        "device id", "deviceid", "agent id", "agentid",
    )
    platform_id_aliases = (
        "platform_id", "platform id", "platformid", "id", "asset id", "assetid",
    )
    hostname_aliases = (
        "hostname", "host name", "device name", "devicename", "computer name",
        "computername", "machine name", "machinename", "name", "friendly name",
        "friendlyname",
    )
    last_contact_aliases = (
        "last contact", "lastcontact", "last contact time", "lastcontacttime",
        "last online", "lastonline", "last online at", "lastonlineat",
        "last seen", "lastseen", "last seen at", "lastseenat",
        "last check in", "lastcheckin", "last check-in", "last checkin time",
        "last activity", "lastactivity", "last reported", "lastreported",
        "last report time", "lastreporttime", "last heartbeat", "lastheartbeat",
    )
    company_aliases = ("company", "company name", "companyname", "client", "client name")
    site_aliases = ("site", "site name", "sitename", "location")

    for line_number, row in enumerate(reader, start=2):
        if len(rows) >= 20000:
            errors.append("Import stopped at 20,000 rows.")
            break

        endpoint_id = _csv_value(row, endpoint_aliases)
        platform_id = _csv_value(row, platform_id_aliases)
        hostname = _csv_value(row, hostname_aliases)
        last_contact = _csv_value(row, last_contact_aliases)
        company_name = normalize_company_name(_csv_value(row, company_aliases))
        site_name = _csv_value(row, site_aliases)

        if not last_contact:
            skipped += 1
            if len(errors) < 10:
                errors.append(f"Line {line_number}: missing last-contact value.")
            continue
        if not endpoint_id and not platform_id and not hostname:
            skipped += 1
            if len(errors) < 10:
                errors.append(f"Line {line_number}: missing endpoint/device ID or hostname.")
            continue

        rows.append({
            "endpoint_id": endpoint_id,
            "platform_id": platform_id,
            "hostname": hostname,
            "hostname_norm": normalize_hostname(hostname),
            "company_name": company_name,
            "site_name": site_name,
            "last_contact": last_contact,
        })

    if not rows:
        raise HTTPException(
            status_code=400,
            detail="No usable rows found. Include Last Contact/Last Online plus Endpoint ID or Hostname columns.",
        )

    repo = AsioLastContactRepository(session)
    result = await repo.bulk_upsert(rows, source_filename=body.filename[:256])
    return {
        "imported": len(rows),
        "inserted": result["inserted"],
        "updated": result["updated"],
        "skipped": skipped,
        "errors": errors,
    }


@router.get("/sc-vs-asio", response_model=list[dict[str, Any]])
async def sc_vs_asio_breakdown(
    session: AsyncSession = Depends(_get_session),
) -> list[dict[str, Any]]:
    """Per-company comparison of ScreenConnect vs Asio device inventories.

    Reads from ``platform_devices`` only — no live API calls. Devices are
    joined on normalised hostname (the most reliable cross-key between SC
    and Asio).  For each company you get:

    - ``sc_count`` / ``asio_count`` — total devices on each platform
    - ``in_both`` — count of hostnames present on both platforms
    - ``sc_only`` — list of devices only in ScreenConnect
    - ``asio_only`` — list of devices only in Asio
    - ``diff`` — ``sc_count - asio_count`` (positive = SC has extras)
    """
    from sqlalchemy import select as sa_select
    from app.db.schema import PlatformDevice as PlatformDeviceRow

    rows = (await session.execute(
        sa_select(PlatformDeviceRow).where(
            PlatformDeviceRow.platform.in_(["screenconnect", "asio"])
        )
    )).scalars().all()

    # company → platform → {hostname_norm: {hostname, platform_id, site_name}}
    grouped: dict[str, dict[str, dict[str, dict[str, str]]]] = defaultdict(
        lambda: {"screenconnect": {}, "asio": {}}
    )
    for r in rows:
        company = r.company_canonical or "(unassigned)"
        hn = r.hostname_norm or r.hostname.lower()
        if not hn:
            continue
        grouped[company][r.platform][hn] = {
            "hostname": r.hostname,
            "platform_id": r.platform_id,
            "site_name": r.site_name,
        }

    out: list[dict[str, Any]] = []
    for company in sorted(grouped.keys()):
        sc_map = grouped[company]["screenconnect"]
        asio_map = grouped[company]["asio"]
        sc_set = set(sc_map.keys())
        asio_set = set(asio_map.keys())
        only_sc = sorted(sc_set - asio_set)
        only_asio = sorted(asio_set - sc_set)
        out.append({
            "company": company,
            "sc_count": len(sc_set),
            "asio_count": len(asio_set),
            "in_both": len(sc_set & asio_set),
            "sc_only_count": len(only_sc),
            "asio_only_count": len(only_asio),
            "diff": len(sc_set) - len(asio_set),
            "sc_only": [sc_map[h] for h in only_sc],
            "asio_only": [asio_map[h] for h in only_asio],
        })

    # Sort: biggest absolute gap first so problem companies bubble up
    out.sort(key=lambda r: -abs(r["diff"]))
    return out


@router.get("/sc-vs-asio-clean", response_model=list[dict[str, Any]])
async def sc_vs_asio_clean(
    session: AsyncSession = Depends(_get_session),
) -> list[dict[str, Any]]:
    """Smart ScreenConnect-vs-Asio comparison, mismatches only.

    Improvements over ``/sc-vs-asio``:

    1. **endpoint_id is tried first** — when both records expose a Windows
       MachineGuid / SID this is an exact, hostname-independent match.
    2. **NetBIOS truncation aware** — ScreenConnect caps hostnames at 15
       characters; we therefore also compare the first 15 chars of the
       normalised hostname (e.g. ``BWLAPTOP1-12-20`` ≡ ``BWLAPTOP1-12-2021``).
    3. **Mismatches only** — companies whose devices fully reconcile are
       omitted, so the report shows only what needs cleanup.
    """
    from sqlalchemy import select as sa_select
    from app.db.schema import PlatformDevice as PlatformDeviceRow

    rows = (await session.execute(
        sa_select(PlatformDeviceRow).where(
            PlatformDeviceRow.platform.in_(["screenconnect", "asio"])
        )
    )).scalars().all()

    (
        csv_last_contact_by_endpoint,
        csv_last_contact_by_platform_id,
        csv_last_contact_by_company_hostname,
        csv_last_contact_by_hostname,
    ) = (
        await AsioLastContactRepository(session).get_lookups()
    )

    def _keys(r) -> tuple[str, str]:  # type: ignore[no-untyped-def]
        """Return (endpoint_id, hostname15) — either may be empty."""
        eid = (r.endpoint_id or "").strip().lower()
        hn = (r.hostname_norm or r.hostname or "").lower().strip()
        return eid, hn[:15]

    # Field names where each platform stashes the device's last-online stamp
    # in the raw_json blob persisted at scan time.  Tried first as a fast path;
    # if nothing matches we fall back to a recursive scan for any key that
    # looks like a "last seen" timestamp.
    _LAST_SEEN_FIELDS: dict[str, tuple[str, ...]] = {
        "screenconnect": (
            "GuestInfoUpdateTime", "GuestLastActivityTime",
            "GuestConnectedTime", "GuestLastSeenAt", "LastConnectedEventTime",
        ),
        "asio": (
            "lastSeenAt", "lastSeen", "lastContact", "lastContactTime",
            "lastCheckIn", "lastCheckin", "lastReportedAt", "agentLastSeenAt",
            "lastActivityAt", "lastConnectedAt", "lastReportTime",
            "lastReportedTime", "lastReported", "lastHeartbeat",
            "lastHeartbeatAt", "lastOnline", "lastOnlineAt",
            "lastCommunication", "lastCommunicationAt",
            "agentLastReported", "lastBootTime", "lastScanTime",
            "lastScannedAt", "lastScannedTime", "lastInventoryScan",
            "lastInventoryScanAt", "lastAgentContact", "lastAgentContactAt",
            "lastStatusUpdate", "lastStatusUpdatedAt", "lastStatusChange",
            "lastStatusChangedAt", "updatedAt", "updatedOn", "modifiedAt",
            "modifiedOn", "dateLastSeen", "lastSeenDate", "lastContactedAt",
            "lastCheckInTime", "lastCheckinTime", "lastSyncTime",
            "lastAccessTime", "lastSuccessfulScan", "lastSuccessfulScanAt",
        ),
    }

    # Substrings that identify a "last seen / online / contact" timestamp when
    # walking unknown JSON.  Combined with a date-ish value check this picks up
    # vendor-specific spellings like ``deviceLastReportTime`` etc.
    _LAST_SEEN_HINTS: tuple[str, ...] = (
        "lastseen", "lastcontact", "lastcheck", "lastreport",
        "lastactivity", "lastconnect", "lastonline", "lastheartbeat",
        "lastcommunication", "lastsync", "lastbeat", "lastscan",
        "laststatus", "lastagent", "datelastseen", "updated", "modified",
    )

    _ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]")

    def _normalise_json_key(key: str) -> str:
        return key.lower().replace("_", "").replace("-", "").replace(" ", "")

    def _coerce_timestamp(val: Any) -> str:
        """Return a JavaScript-parseable timestamp string, or empty if invalid."""
        if val is None or isinstance(val, bool):
            return ""
        if isinstance(val, (int, float)):
            if val <= 1_000_000_000:
                return ""
            seconds = val / 1000 if val > 10_000_000_000 else val
            try:
                return datetime.fromtimestamp(seconds, UTC).isoformat()
            except (OverflowError, OSError, ValueError):
                return ""
        if isinstance(val, str):
            v = val.strip()
            if not v or v.lower() in {"never", "n/a", "null", "none", "0", "1970-01-01t00:00:00z"}:
                return ""
            if v.isdigit():
                return _coerce_timestamp(int(v))
            match = re.search(r"/Date\((\d+)\)/", v)
            if match:
                return _coerce_timestamp(int(match.group(1)))
            if _ISO_RE.match(v):
                return v
            # Keep other date-ish strings as a last resort. Browser Date parsing
            # can handle common API formats like "01/02/2025 03:04 PM".
            if re.search(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}", v):
                return v
        return ""

    def _looks_like_timestamp(val: Any) -> bool:
        return bool(_coerce_timestamp(val))

    def _recursive_last_seen(node: Any, depth: int = 0) -> str:
        """Walk a JSON tree looking for any key matching a last-seen hint
        whose value looks like a timestamp.  Bounded depth to stay cheap.
        """
        if depth > 4 or node is None:
            return ""
        if isinstance(node, dict):
            for key, val in node.items():
                key_l = _normalise_json_key(key)
                coerced = _coerce_timestamp(val)
                if any(h in key_l for h in _LAST_SEEN_HINTS) and coerced:
                    return coerced
            for val in node.values():
                if isinstance(val, (dict, list)):
                    found = _recursive_last_seen(val, depth + 1)
                    if found:
                        return found
        elif isinstance(node, list):
            for item in node[:20]:  # cap list scan
                found = _recursive_last_seen(item, depth + 1)
                if found:
                    return found
        return ""

    def _extract_last_seen(platform: str, raw: dict | None) -> str:
        """Pull a last-online timestamp out of raw_json, best-effort.

        Strategy:
        1. Try the known per-platform field names at the top level.
        2. Try the same names inside common nested containers.
        3. Fall back to a recursive scan for any key matching a last-seen hint.
        """
        if not raw or not isinstance(raw, dict):
            return ""
        candidates = {_normalise_json_key(key) for key in _LAST_SEEN_FIELDS.get(platform, ())}

        raw_by_normalised_key = {_normalise_json_key(str(key)): val for key, val in raw.items()}
        for key in candidates:
            coerced = _coerce_timestamp(raw_by_normalised_key.get(key))
            if coerced:
                return coerced
        for container_key in ("agentConnection", "connection", "status", "system",
                              "agent", "device", "lastEvent", "endpoint"):
            nested = raw.get(container_key)
            if isinstance(nested, dict):
                nested_by_normalised_key = {_normalise_json_key(str(key)): val for key, val in nested.items()}
                for key in candidates:
                    coerced = _coerce_timestamp(nested_by_normalised_key.get(key))
                    if coerced:
                        return coerced
        return _recursive_last_seen(raw)

    # company → platform → list of (eid, hn15, raw_dict)
    grouped: dict[str, dict[str, list[tuple[str, str, dict[str, str]]]]] = defaultdict(
        lambda: {"screenconnect": [], "asio": []}
    )
    for r in rows:
        company = r.company_canonical or "(unassigned)"
        eid, hn15 = _keys(r)
        if not eid and not hn15:
            continue
        last_seen = _extract_last_seen(r.platform, r.raw_json)
        if r.platform == "asio" and not last_seen:
            hostname_key = (r.hostname_norm or r.hostname or "").lower()
            company_hostname_key = f"{company.lower()}|{hostname_key}" if hostname_key else ""
            last_seen = (
                csv_last_contact_by_endpoint.get((r.endpoint_id or "").lower())
                or csv_last_contact_by_platform_id.get((r.platform_id or "").lower())
                or csv_last_contact_by_company_hostname.get(company_hostname_key)
                or csv_last_contact_by_hostname.get(hostname_key)
                or ""
            )
        grouped[company][r.platform].append((eid, hn15, {
            "hostname": r.hostname,
            "platform_id": r.platform_id,
            "endpoint_id": r.endpoint_id,
            "site_name": r.site_name,
            "last_seen": last_seen,
            "last_seen_db": r.last_seen_at.isoformat() if r.last_seen_at else "",
        }))

    out: list[dict[str, Any]] = []
    for company in sorted(grouped.keys()):
        sc_list = grouped[company]["screenconnect"]
        asio_list = grouped[company]["asio"]

        # Build Asio lookup sets
        asio_eids = {eid for eid, _, _ in asio_list if eid}
        asio_hn15 = {hn for _, hn, _ in asio_list if hn}
        sc_eids = {eid for eid, _, _ in sc_list if eid}
        sc_hn15 = {hn for _, hn, _ in sc_list if hn}

        # A device is "matched" if its eid OR hn15 appears on the other side
        sc_only: list[dict[str, str]] = []
        for eid, hn15, raw in sc_list:
            matched = (eid and eid in asio_eids) or (hn15 and hn15 in asio_hn15)
            if not matched:
                sc_only.append(raw)

        asio_only: list[dict[str, str]] = []
        for eid, hn15, raw in asio_list:
            matched = (eid and eid in sc_eids) or (hn15 and hn15 in sc_hn15)
            if not matched:
                asio_only.append(raw)

        # Skip companies with nothing to flag
        if not sc_only and not asio_only:
            continue

        sc_only.sort(key=lambda d: d["hostname"].lower())
        asio_only.sort(key=lambda d: d["hostname"].lower())

        out.append({
            "company": company,
            "sc_total": len(sc_list),
            "asio_total": len(asio_list),
            "missing_from_asio": sc_only,    # in SC, not in Asio
            "missing_from_sc": asio_only,    # in Asio, not in SC
            "missing_from_asio_count": len(sc_only),
            "missing_from_sc_count": len(asio_only),
        })

    # Worst offenders first
    out.sort(key=lambda r: -(r["missing_from_asio_count"] + r["missing_from_sc_count"]))
    return out


@router.get("/sc-vs-asio-clean/export")
async def export_sc_vs_asio_clean_csv(
    session: AsyncSession = Depends(_get_session),
) -> StreamingResponse:
    """Human-readable CSV of mismatches only, grouped by company.

    Layout (blank lines separate companies):

    ```
    Company,SC Total,Asio Total,Missing from Asio,Missing from SC
    roberts and company,2,2,2,2

    ,Missing from ASIO (in SC),,,
    ,RCDESKTOP1-1120,(SC platform_id),(endpoint_id),(site)
    ,RCDESKTOP2-1120,...

    ,Missing from SC (in Asio),,,
    ,RCDESKTOP1-112025,...
    ```
    """
    data = _sorted_sc_asio_export(await sc_vs_asio_clean(session=session))

    buf = io.StringIO()
    writer = csv.writer(buf)

    for company in data:
        name = company["company"]
        writer.writerow(["Company", "SC Total", "Asio Total",
                         "Missing from Asio", "Missing from SC"])
        writer.writerow([
            name,
            company["sc_total"],
            company["asio_total"],
            company["missing_from_asio_count"],
            company["missing_from_sc_count"],
        ])

        if company["missing_from_asio"]:
            writer.writerow([])
            writer.writerow(["", "Missing from ASIO (present in ScreenConnect)",
                             "SC Session ID", "Endpoint ID", "Site",
                              "Offline Age (SC)", "Last Seen Source"])
            for d in company["missing_from_asio"]:
                writer.writerow(["", d["hostname"], d["platform_id"],
                                 d["endpoint_id"], d["site_name"],
                                  _format_offline_age(d.get("last_seen", "")) or "Unknown",
                                  "API" if d.get("last_seen") else "DB scan time"])

        if company["missing_from_sc"]:
            writer.writerow([])
            writer.writerow(["", "Missing from ScreenConnect (present in Asio)",
                             "Asio Device ID", "Endpoint ID", "Site",
                              "Offline Age (Asio)", "Last Seen Source"])
            for d in company["missing_from_sc"]:
                writer.writerow(["", d["hostname"], d["platform_id"],
                                 d["endpoint_id"], d["site_name"],
                                  _format_offline_age(d.get("last_seen", "")) or "Unknown",
                                  "API/CSV" if d.get("last_seen") else "DB scan time"])

        writer.writerow([])
        writer.writerow([])

    buf.seek(0)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="sc_vs_asio_clean_{timestamp}.csv"'
        },
    )


@router.get("/sc-vs-asio-clean/export.xls")
async def export_sc_vs_asio_clean_excel(
    session: AsyncSession = Depends(_get_session),
) -> StreamingResponse:
    """Excel-readable highlighted export of SC-vs-Asio mismatches."""
    data = _sorted_sc_asio_export(await sc_vs_asio_clean(session=session))

    def _cell(value: Any, style: str = "") -> str:
        attr = f' style="{style}"' if style else ""
        return f"<td{attr}>{html.escape(str(value or ''))}</td>"

    def _age_style(value: Any) -> str:
        days = _offline_age_days(value)
        if days is None:
            return "background:#e5e7eb;color:#374151;"
        if days > 30:
            return "background:#fecaca;color:#7f1d1d;font-weight:bold;"
        if days > 7:
            return "background:#fef3c7;color:#78350f;font-weight:bold;"
        return "background:#dcfce7;color:#14532d;font-weight:bold;"

    company_style = "background:#dbeafe;color:#1e3a8a;font-weight:bold;"
    sc_style = "background:#fee2e2;color:#7f1d1d;font-weight:bold;"
    asio_style = "background:#ffedd5;color:#7c2d12;font-weight:bold;"
    header_style = "background:#1e293b;color:#ffffff;font-weight:bold;"

    parts = [
        "<html><head><meta charset='utf-8'>",
        "<style>table{border-collapse:collapse;font-family:Segoe UI,Arial,sans-serif;font-size:12px;}td,th{border:1px solid #cbd5e1;padding:5px 8px;} .spacer td{border:none;height:12px;}</style>",
        "</head><body><table>",
    ]

    for company in data:
        parts.append("<tr>")
        parts.append(_cell("Company", header_style))
        parts.append(_cell(company["company"], company_style))
        parts.append(_cell("SC Total", header_style))
        parts.append(_cell(company["sc_total"]))
        parts.append(_cell("Asio Total", header_style))
        parts.append(_cell(company["asio_total"]))
        parts.append(_cell("Missing from Asio", header_style))
        parts.append(_cell(company["missing_from_asio_count"]))
        parts.append(_cell("Missing from SC", header_style))
        parts.append(_cell(company["missing_from_sc_count"]))
        parts.append("</tr>")

        if company["missing_from_asio"]:
            parts.append(f"<tr>{_cell('Missing from ASIO (present in ScreenConnect)', sc_style)}</tr>")
            parts.append("<tr>" + "".join(_cell(h, header_style) for h in [
                "Hostname", "SC Session ID", "Endpoint ID", "Site", "Offline Age (SC)", "Source",
            ]) + "</tr>")
            for d in company["missing_from_asio"]:
                parts.append("<tr>")
                parts.append(_cell(d.get("hostname", "")))
                parts.append(_cell(d.get("platform_id", "")))
                parts.append(_cell(d.get("endpoint_id", "")))
                parts.append(_cell(d.get("site_name", "")))
                parts.append(_cell(_format_offline_age(d.get("last_seen", "")) or "Unknown", _age_style(d.get("last_seen", ""))))
                parts.append(_cell("API" if d.get("last_seen") else "DB scan time"))
                parts.append("</tr>")

        if company["missing_from_sc"]:
            parts.append(f"<tr>{_cell('Missing from ScreenConnect (present in Asio)', asio_style)}</tr>")
            parts.append("<tr>" + "".join(_cell(h, header_style) for h in [
                "Hostname", "Asio Device ID", "Endpoint ID", "Site", "Offline Age (Asio)", "Source",
            ]) + "</tr>")
            for d in company["missing_from_sc"]:
                parts.append("<tr>")
                parts.append(_cell(d.get("hostname", "")))
                parts.append(_cell(d.get("platform_id", "")))
                parts.append(_cell(d.get("endpoint_id", "")))
                parts.append(_cell(d.get("site_name", "")))
                parts.append(_cell(_format_offline_age(d.get("last_seen", "")) or "Unknown", _age_style(d.get("last_seen", ""))))
                parts.append(_cell("API/CSV" if d.get("last_seen") else "DB scan time"))
                parts.append("</tr>")

        parts.append("<tr class='spacer'><td colspan='10'></td></tr>")

    parts.append("</table></body></html>")
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter(["".join(parts)]),
        media_type="application/vnd.ms-excel",
        headers={
            "Content-Disposition": f'attachment; filename="sc_vs_asio_clean_{timestamp}.xls"'
        },
    )


@router.get("/sc-vs-asio/export")
async def export_sc_vs_asio_csv(
    session: AsyncSession = Depends(_get_session),
) -> StreamingResponse:
    """CSV download of the ScreenConnect-vs-Asio per-company breakdown."""
    data = await sc_vs_asio_breakdown(session=session)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Company", "Platform", "Status", "Hostname", "Platform ID", "Site",
    ])
    for row in data:
        company = row["company"]
        for d in row["sc_only"]:
            writer.writerow([company, "screenconnect", "sc_only",
                             d["hostname"], d["platform_id"], d["site_name"]])
        for d in row["asio_only"]:
            writer.writerow([company, "asio", "asio_only",
                             d["hostname"], d["platform_id"], d["site_name"]])

    buf.seek(0)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="sc_vs_asio_{timestamp}.csv"'
        },
    )


# ── Reconciled Devices — Cross-Platform Matched View ──────────────


@router.get(
    "/reconciled",
    response_model=list[dict[str, Any]],
    summary="Cross-platform reconciled device inventory per company (DB-backed, fast)",
)
async def company_reconciled(
    session: AsyncSession = Depends(_get_session),
    force: bool = False,
) -> list[dict[str, Any]]:
    """Cross-platform device reconciliation using DB-stored device inventory.

    Uses endpoint_id as the primary join key (ASIO ↔ PSA) and normalised
    hostname as a fallback (ScreenConnect, SentinelOne).  Data must be
    populated first via ``POST /api/companies/scan``.

    If the ``platform_devices`` table is empty, falls back to the legacy
    live-API reconciliation so existing behaviour is preserved until the
    first scan completes.

    Per-company AV assignment is determined from PSA agreement additions.
    ``assigned_av`` will be ``"eset"``, ``"sentinelone"``, ``"both"``, or
    ``"none"`` based on the products attached to each company's managed-services
    agreement.

    Pass ``?force=true`` to bypass the 15-minute result cache.
    """
    # ── Check in-memory cache ──
    if not force and _reconciled_cache["ts"] is not None:
        age = (datetime.now(UTC) - _reconciled_cache["ts"]).total_seconds()
        if age < _CACHE_TTL_SECONDS and _reconciled_cache["data"]:
            return _reconciled_cache["data"]

    # ── Check breakdown_cache table (pre-computed by reconcile run) ──
    bk_repo = BreakdownCacheRepository(session)
    bk_rows = await bk_repo.get_all()
    if bk_rows and not force:
        reconciled = [row.data for row in bk_rows if row.data]
        _reconciled_cache["ts"] = datetime.now(UTC)
        _reconciled_cache["data"] = reconciled
        log.info("company_reconciled.from_breakdown_cache", companies=len(reconciled))
        return reconciled

    # ── No pre-computed cache — fall back to live computation ──
    device_repo = PlatformDeviceRepository(session)
    all_db_devices = await device_repo.list_all()

    if not all_db_devices:
        # No DB data yet — fall back to legacy live reconciliation
        # Apply overall timeout so the endpoint never hangs indefinitely
        try:
            return await asyncio.wait_for(
                _legacy_reconciled(session),
                timeout=180.0,
            )
        except asyncio.TimeoutError:
            log.error(
                "company_reconciled.legacy_timeout",
                detail="Legacy reconciliation exceeded 180s — returning empty",
            )
            return _reconciled_cache.get("data") or []

    connectors = get_connectors()

    # ── ESET presence + AV map (still fetched live as company-level data) ──
    alias_repo = CompanyAliasRepository(session)
    alias_map = await alias_repo.get_alias_map()

    # Fetch ESET companies for presence matching
    eset_normalised_names: set[str] = set()
    eset_error: str | None = None
    for c in connectors:
        if c.platform_name == "eset":
            try:
                eset_companies = await asyncio.wait_for(
                    c.get_companies(), timeout=90.0,
                )
                for comp in eset_companies:
                    norm = comp.name
                    if norm:
                        norm = alias_map.get(norm, norm)
                        eset_normalised_names.add(norm)
            except asyncio.TimeoutError:
                eset_error = "ESET get_companies timed out (90s)"
                log.warning("reconciled.eset_timeout", error=eset_error)
            except Exception as exc:  # noqa: BLE001
                eset_error = str(exc)
                log.warning(
                    "reconciled.eset_unavailable",
                    error=eset_error,
                )
            break

    # AV map from PSA
    av_map: dict[str, str] = {}
    psa = _get_psa_connector()
    if psa:
        try:
            raw_av = await asyncio.wait_for(
                psa.get_company_av_map(), timeout=90.0,
            )
            for name, av in raw_av.items():
                av_map[name] = av
        except asyncio.TimeoutError:
            log.error("reconciled.av_map_timeout", timeout=90.0)
        except Exception:  # noqa: BLE001
            pass

    # ── Load per-device AV detection data ──
    av_repo = DeviceAvStatusRepository(session)
    av_lookup = await av_repo.get_av_lookup()

    # ── Reconcile from DB using endpoint-ID-first matching ──
    # CPU-bound — off the event loop so other requests keep being answered
    reconciled = await asyncio.to_thread(reconcile_from_db, all_db_devices, av_lookup=av_lookup)

    # Flag each company with PSA presence (no longer filter out non-PSA companies)
    def _has_psa(company_data: dict[str, Any]) -> bool:
        """Check if company has PSA presence (devices OR agreement)."""
        # Check for PSA devices
        for dev in company_data.get("matched", []):
            if "connectwise_psa" in dev.get("platforms", []):
                return True
        if "connectwise_psa" in company_data.get("mismatched", {}):
            return True
        # Also check if company has PSA agreement (even if no devices yet)
        # This handles S1-only companies with agreements but no configured devices
        company_name = company_data.get("company", "")
        if company_name in av_map:
            return True
        return False

    for company_data in reconciled:
        company_data["psa_present"] = _has_psa(company_data)

    # ── Enrich with live ESET Connect (Protect Cloud) device data ──
    try:
        eset_hn, eset_mac, eset_serial = await asyncio.wait_for(
            _build_eset_connect_lookups(), timeout=90.0,
        )
    except asyncio.TimeoutError:
        log.error("reconciled.eset_connect_timeout", timeout=90.0)
        eset_hn, eset_mac, eset_serial = {}, {}, {}
    for company_data in reconciled:
        for dev in company_data.get("matched", []):
            _enrich_device_with_eset_connect(dev, eset_hn, eset_mac, eset_serial)
        for _plat, devs in company_data.get("mismatched", {}).items():
            for dev in devs:
                _enrich_device_with_eset_connect(dev, eset_hn, eset_mac, eset_serial)

    # ── Annotate with ESET / S1 / AV info ──
    for company_data in reconciled:
        company_name = company_data["company"]
        # Check ESET presence from the MSP Portal company list first
        eset_from_portal = company_name in eset_normalised_names
        # Also derive ESET presence from per-device AV detection data
        eset_from_devices = any(
            "eset" in dev.get("av_vendors", [])
            for dev in company_data.get("matched", [])
        ) or any(
            "eset" in dev.get("av_vendors", [])
            for devs in company_data.get("mismatched", {}).values()
            for dev in devs
        )
        company_data["eset_present"] = eset_from_portal or eset_from_devices
        has_s1 = any(
            "sentinelone" in dev.get("platforms", [])
            for dev in company_data.get("matched", [])
        ) or "sentinelone" in company_data.get("mismatched", {})
        company_data["s1_present"] = has_s1
        company_data["assigned_av"] = av_map.get(company_name, "none")

        # Per-device endpoint_protection tagging — uses assigned_av from PSA
        # to enforce one-AV-per-company rule.
        assigned = company_data["assigned_av"]
        for dev in company_data.get("matched", []):
            dev["endpoint_protection"] = _compute_endpoint_protection(dev, assigned)
        for _plat_name, devs in company_data.get("mismatched", {}).items():
            for dev in devs:
                dev["endpoint_protection"] = _compute_endpoint_protection(dev, assigned)

    # Persist to reconciled_devices table
    db_records: list[ReconciledDevice] = []
    for company_data in reconciled:
        company_name = _sanitize_for_pg(company_data["company"])
        for dev in company_data["matched"]:
            db_records.append(ReconciledDevice(
                id=str(uuid4()),
                company_name=company_name,
                site_name=_sanitize_for_pg(dev.get("site_name", ""))[:512],
                hostname=_sanitize_for_pg(dev["hostname"]),
                endpoint_id=_sanitize_for_pg(dev.get("endpoint_id", ""))[:128],
                mac_address=_sanitize_for_pg(dev.get("mac_address", ""))[:256],
                serial_number=_sanitize_for_pg(dev.get("serial_number", ""))[:256],
                platforms=dev["platforms"],
                match_type=dev["match_type"],
            ))
        for _plat, devs in company_data.get("mismatched", {}).items():
            for dev in devs:
                db_records.append(ReconciledDevice(
                    id=str(uuid4()),
                    company_name=company_name,
                    site_name=_sanitize_for_pg(dev.get("site_name", ""))[:512],
                    hostname=_sanitize_for_pg(dev["hostname"]),
                    endpoint_id=_sanitize_for_pg(dev.get("endpoint_id", ""))[:128],
                    mac_address=_sanitize_for_pg(dev.get("mac_address", ""))[:256],
                    serial_number=_sanitize_for_pg(dev.get("serial_number", ""))[:256],
                    platforms=dev["platforms"],
                    match_type=dev["match_type"],
                ))

    repo = ReconciledDeviceRepository(session)
    await repo.replace_all(db_records)

    # Update cache
    _reconciled_cache["ts"] = datetime.now(UTC)
    _reconciled_cache["data"] = reconciled
    _reconciled_cache["av_map"] = av_map

    return reconciled


async def _legacy_reconciled(session: AsyncSession) -> list[dict[str, Any]]:
    """Fallback: live-API reconciliation when platform_devices table is empty.

    Preserved for backward compatibility until the first scan populates the
    DB.  Uses the old hostname-only ``reconcile_devices()`` path.
    """
    t0 = time.monotonic()
    connectors = get_connectors()
    if not connectors:
        return []

    log.info("legacy_reconciled.start", connectors=[c.platform_name for c in connectors])

    alias_repo = CompanyAliasRepository(session)
    alias_map = await alias_repo.get_alias_map()

    _STEP_TIMEOUT = 90.0  # per-step timeout for external calls

    async def _safe_devices(connector):  # type: ignore[no-untyped-def]
        try:
            devices = await asyncio.wait_for(
                connector.get_devices(), timeout=_STEP_TIMEOUT,
            )
            log.info(
                "legacy_reconciled.devices_ok",
                platform=connector.platform_name,
                count=len(devices),
            )
            return connector.platform_name, devices
        except asyncio.TimeoutError:
            log.error(
                "legacy_reconciled.device_timeout",
                platform=connector.platform_name,
                timeout=_STEP_TIMEOUT,
            )
            return connector.platform_name, []
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "legacy_reconciled.device_fetch_error",
                platform=connector.platform_name,
                error=str(exc),
            )
            return connector.platform_name, []

    async def _safe_companies(connector):  # type: ignore[no-untyped-def]
        try:
            companies = await asyncio.wait_for(
                connector.get_companies(), timeout=_STEP_TIMEOUT,
            )
            log.info(
                "legacy_reconciled.companies_ok",
                platform=connector.platform_name,
                count=len(companies),
            )
            return connector.platform_name, companies
        except asyncio.TimeoutError:
            log.error(
                "legacy_reconciled.company_timeout",
                platform=connector.platform_name,
                timeout=_STEP_TIMEOUT,
            )
            return connector.platform_name, []
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "legacy_reconciled.company_fetch_error",
                platform=connector.platform_name,
                error=str(exc),
            )
            return connector.platform_name, []

    t1 = time.monotonic()
    results = await asyncio.gather(*[_safe_devices(c) for c in connectors])
    log.info("legacy_reconciled.devices_done", elapsed=f"{time.monotonic() - t1:.1f}s")

    t2 = time.monotonic()
    company_results = await asyncio.gather(*[_safe_companies(c) for c in connectors])
    log.info("legacy_reconciled.companies_done", elapsed=f"{time.monotonic() - t2:.1f}s")

    companies_by_platform: dict[str, list] = {}
    for plat, companies in company_results:
        companies_by_platform.setdefault(plat, []).extend(companies)

    auto_aliases = await asyncio.to_thread(build_cross_platform_aliases,companies_by_platform, alias_map)
    device_aliases = await asyncio.to_thread(build_device_cross_reference_aliases,list(results), alias_map)
    merged_alias_map = {**device_aliases, **auto_aliases, **alias_map}

    eset_normalised_names: set[str] = set()
    for comp in companies_by_platform.get("eset", []):
        norm = comp.name
        if norm:
            norm = merged_alias_map.get(norm, norm)
            eset_normalised_names.add(norm)

    all_companies: set[str] = set()
    for _platform, devices in results:
        for dev in devices:
            company = dev.company_name or dev.company_platform_id or ""
            if company:
                norm = normalize_company_name(company)
                if not norm:
                    continue
                norm = merged_alias_map.get(norm, norm)
                all_companies.add(norm)

    count_map: dict[str, dict[str, int]] = {c: {"_t": 1} for c in all_companies}
    merged_count = merge_prefix_duplicates(count_map)
    prefix_canonical: dict[str, str] = {}
    merged_words: dict[str, str] = {}
    for canon in merged_count:
        prefix = " ".join(canon.split()[:3])
        merged_words[prefix] = canon
    for comp in all_companies:
        prefix = " ".join(comp.split()[:3])
        prefix_canonical[comp] = merged_words.get(prefix, comp)

    eset_canonical: set[str] = set()
    for name in eset_normalised_names:
        eset_canonical.add(prefix_canonical.get(name, name))
        prefix = " ".join(name.split()[:3])
        if prefix in merged_words:
            eset_canonical.add(merged_words[prefix])

    log.info("legacy_reconciled.alias_processing_done", companies=len(all_companies))

    av_map: dict[str, str] = {}
    psa = _get_psa_connector()
    if psa:
        try:
            t3 = time.monotonic()
            raw_av = await asyncio.wait_for(
                psa.get_company_av_map(), timeout=_STEP_TIMEOUT,
            )
            log.info(
                "legacy_reconciled.av_map_done",
                count=len(raw_av),
                elapsed=f"{time.monotonic() - t3:.1f}s",
            )
            for name, av in raw_av.items():
                canon = prefix_canonical.get(name, name)
                av_map[canon] = av
                prefix = " ".join(name.split()[:3])
                if prefix in merged_words:
                    av_map[merged_words[prefix]] = av
        except asyncio.TimeoutError:
            log.error("legacy_reconciled.av_map_timeout", timeout=_STEP_TIMEOUT)
        except Exception:  # noqa: BLE001
            log.warning("legacy_reconciled.av_map_error")

    t4 = time.monotonic()
    total_devices = sum(len(devs) for _, devs in results)
    log.info("legacy_reconciled.reconcile_start", total_devices=total_devices)
    reconciled = reconcile_devices(
        platform_devices=list(results),
        alias_map=merged_alias_map,
        prefix_canonical=prefix_canonical,
    )
    log.info(
        "legacy_reconciled.reconcile_done",
        companies=len(reconciled),
        elapsed=f"{time.monotonic() - t4:.1f}s",
    )

    def _has_psa_legacy(company_data: dict[str, Any]) -> bool:
        """Check if company has PSA presence (devices OR agreement)."""
        # Check for PSA devices
        for dev in company_data.get("matched", []):
            if "connectwise_psa" in dev.get("platforms", []):
                return True
        if "connectwise_psa" in company_data.get("mismatched", {}):
            return True
        # Also check if company has PSA agreement (even if no devices yet)
        company_name = company_data.get("company", "")
        if company_name in av_map:
            return True
        return False

    for company_data in reconciled:
        company_data["psa_present"] = _has_psa_legacy(company_data)

    # ── Enrich with live ESET Connect (Protect Cloud) device data ──
    t5 = time.monotonic()
    try:
        eset_hn, eset_mac, eset_serial = await asyncio.wait_for(
            _build_eset_connect_lookups(), timeout=_STEP_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.error("legacy_reconciled.eset_connect_timeout", timeout=_STEP_TIMEOUT)
        eset_hn, eset_mac, eset_serial = {}, {}, {}
    log.info("legacy_reconciled.eset_connect_done", elapsed=f"{time.monotonic() - t5:.1f}s")

    for company_data in reconciled:
        for dev in company_data.get("matched", []):
            _enrich_device_with_eset_connect(dev, eset_hn, eset_mac, eset_serial)
        for _plat, devs in company_data.get("mismatched", {}).items():
            for dev in devs:
                _enrich_device_with_eset_connect(dev, eset_hn, eset_mac, eset_serial)

    for company_data in reconciled:
        company_name = company_data["company"]
        # Check ESET presence from the MSP Portal company list first
        eset_from_portal = company_name in eset_canonical
        # Also derive ESET presence from per-device AV detection data
        eset_from_devices = any(
            "eset" in dev.get("av_vendors", [])
            for dev in company_data.get("matched", [])
        ) or any(
            "eset" in dev.get("av_vendors", [])
            for devs in company_data.get("mismatched", {}).values()
            for dev in devs
        )
        company_data["eset_present"] = eset_from_portal or eset_from_devices
        has_s1 = any(
            "sentinelone" in dev.get("platforms", [])
            for dev in company_data.get("matched", [])
        ) or "sentinelone" in company_data.get("mismatched", {})
        company_data["s1_present"] = has_s1
        company_data["assigned_av"] = av_map.get(company_name, "none")

        assigned = company_data["assigned_av"]
        for dev in company_data.get("matched", []):
            dev["endpoint_protection"] = _compute_endpoint_protection(dev, assigned)
        for _plat_name, devs in company_data.get("mismatched", {}).items():
            for dev in devs:
                dev["endpoint_protection"] = _compute_endpoint_protection(dev, assigned)

    db_records: list[ReconciledDevice] = []
    for company_data in reconciled:
        company_name = _sanitize_for_pg(company_data["company"])
        for dev in company_data["matched"]:
            db_records.append(ReconciledDevice(
                id=str(uuid4()),
                company_name=company_name,
                site_name=_sanitize_for_pg(dev.get("site_name", ""))[:512],
                hostname=_sanitize_for_pg(dev["hostname"]),
                endpoint_id=_sanitize_for_pg(dev.get("endpoint_id", ""))[:128],
                mac_address=_sanitize_for_pg(dev.get("mac_address", ""))[:256],
                serial_number=_sanitize_for_pg(dev.get("serial_number", ""))[:256],
                platforms=dev["platforms"],
                match_type=dev["match_type"],
            ))
        for _plat, devs in company_data.get("mismatched", {}).items():
            for dev in devs:
                db_records.append(ReconciledDevice(
                    id=str(uuid4()),
                    company_name=company_name,
                    site_name=_sanitize_for_pg(dev.get("site_name", ""))[:512],
                    hostname=_sanitize_for_pg(dev["hostname"]),
                    endpoint_id=_sanitize_for_pg(dev.get("endpoint_id", ""))[:128],
                    mac_address=_sanitize_for_pg(dev.get("mac_address", ""))[:256],
                    serial_number=_sanitize_for_pg(dev.get("serial_number", ""))[:256],
                    platforms=dev["platforms"],
                    match_type=dev["match_type"],
                ))

    t6 = time.monotonic()
    repo = ReconciledDeviceRepository(session)
    await repo.replace_all(db_records)
    log.info(
        "legacy_reconciled.db_persist_done",
        records=len(db_records),
        elapsed=f"{time.monotonic() - t6:.1f}s",
    )

    _reconciled_cache["ts"] = datetime.now(UTC)
    _reconciled_cache["data"] = reconciled
    _reconciled_cache["av_map"] = av_map

    log.info(
        "legacy_reconciled.complete",
        companies=len(reconciled),
        total_elapsed=f"{time.monotonic() - t0:.1f}s",
    )
    return reconciled


# ── CSV Export ────────────────────────────────────────────────────


@router.get("/reconciled/export")
async def export_reconciled_csv(
    session: AsyncSession = Depends(_get_session),
) -> StreamingResponse:
    """Export the most recent reconciled breakdown as a CSV download.

    Uses cached data if available; otherwise fetches fresh data.
    Each row is one device with company, AV assignment, platform info.
    """
    # Use cached data if fresh, otherwise trigger a fresh reconciliation
    data = _reconciled_cache.get("data")
    if not data:
        data = await company_reconciled(session=session, force=True)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Company",
        "Hostname",
        "Endpoint Protection",
        "Endpoint ID",
        "MAC Address",
        "Serial Number",
        "Site",
        "Platforms",
        "In_PSA",
        "In_S1",
        "In_SC",
        "In_ASIO",
        "Match Type",
    ])

    for company in data:
        name = company.get("company", "")

        def _write_row(dev: dict, match_type: str, plat_str: str) -> None:
            platforms = dev.get("platforms", [])
            writer.writerow([
                name,
                dev.get("hostname", ""),
                dev.get("endpoint_protection", ""),
                dev.get("endpoint_id", ""),
                dev.get("mac_address", ""),
                dev.get("serial_number", ""),
                dev.get("site_name", ""),
                plat_str,
                "Yes" if "connectwise_psa" in platforms else "No",
                "Yes" if "sentinelone" in platforms else "No",
                "Yes" if "screenconnect" in platforms else "No",
                "Yes" if "asio" in platforms else "No",
                match_type,
            ])

        for dev in company.get("matched", []):
            _write_row(dev, dev.get("match_type", ""), " | ".join(dev.get("platforms", [])))

        for plat, devs in company.get("mismatched", {}).items():
            for dev in devs:
                _write_row(dev, "unmatched", plat)

    buf.seek(0)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="reconciled_breakdown_{timestamp}.csv"'
        },
    )


@router.get("/reconciled/export-all-companies")
async def export_all_companies_zip(
    session: AsyncSession = Depends(_get_session),
) -> StreamingResponse:
    """Export ALL companies as separate CSV files bundled in a ZIP.

    Each company gets its own CSV with the same format as the single-company
    export (Summary section + detailed device breakdown). Perfect for printing
    individual company reports.
    """
    import zipfile

    data = _reconciled_cache.get("data")
    if not data:
        data = await company_reconciled(session=session, force=True)

    # Create ZIP file in memory
    zip_buffer = io.BytesIO()
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for company in data:
            name = company.get("company", "")
            if not name:
                continue

            # Create individual CSV for this company
            csv_buffer = io.StringIO()
            writer = csv.writer(csv_buffer)

            # Summary header
            writer.writerow(["Company Report", name])
            writer.writerow(["Matched Devices", company.get("matched_count", 0)])
            writer.writerow(["Mismatched Devices", company.get("mismatched_count", 0)])
            writer.writerow(["PSA Present", "Yes" if company.get("psa_present") else "No"])
            writer.writerow(["SentinelOne Present", "Yes" if company.get("s1_present") else "No"])
            writer.writerow(["ESET Present", "Yes" if company.get("eset_present") else "No"])
            writer.writerow(["Assigned AV", company.get("assigned_av", "none")])
            writer.writerow([])

            # Device table
            writer.writerow([
                "Hostname",
                "Endpoint Protection",
                "Match Type",
                "Platforms",
                "In_PSA",
                "In_S1",
                "In_SC",
                "In_ASIO",
                "Endpoint ID",
                "MAC Address",
                "Serial Number",
                "Site",
            ])

            def _write_device(dev: dict, match_type: str) -> None:
                platforms = dev.get("platforms", [])
                plat_str = " | ".join(platforms)
                writer.writerow([
                    dev.get("hostname", ""),
                    dev.get("endpoint_protection", ""),
                    match_type,
                    plat_str,
                    "Yes" if "connectwise_psa" in platforms else "No",
                    "Yes" if "sentinelone" in platforms else "No",
                    "Yes" if "screenconnect" in platforms else "No",
                    "Yes" if "asio" in platforms else "No",
                    dev.get("endpoint_id", ""),
                    dev.get("mac_address", ""),
                    dev.get("serial_number", ""),
                    dev.get("site_name", ""),
                ])

            for dev in company.get("matched", []):
                _write_device(dev, dev.get("match_type", "matched"))

            for plat, devs in company.get("mismatched", {}).items():
                for dev in devs:
                    _write_device(dev, "unmatched")

            # Add CSV to ZIP with safe filename
            safe_name = "".join(c if c.isalnum() or c in (" ", "-", "_") else "_" for c in name)[:50]
            filename = f"{safe_name}_{timestamp}.csv"
            zip_file.writestr(filename, csv_buffer.getvalue())

    # Return ZIP file
    zip_buffer.seek(0)
    return StreamingResponse(
        iter([zip_buffer.getvalue()]),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="all_companies_{timestamp}.zip"'
        },
    )


@router.get("/reconciled/export/{company_name}")
async def export_company_csv(
    company_name: str,
    session: AsyncSession = Depends(_get_session),
) -> StreamingResponse:
    """Export a single company's reconciled breakdown as a CSV download.

    Includes derived per-platform boolean columns (In_PSA, In_S1, In_SC,
    In_ASIO) for easy filtering, plus a Summary section at the top.
    """
    data = _reconciled_cache.get("data")
    if not data:
        data = await company_reconciled(session=session, force=True)

    # Find company (case-insensitive match)
    company = next(
        (c for c in data if c.get("company", "").lower() == company_name.lower()),
        None,
    )
    if not company:
        raise HTTPException(status_code=404, detail=f"Company '{company_name}' not found in reconciled data")

    name = company.get("company", "")

    buf = io.StringIO()
    writer = csv.writer(buf)

    # Summary header
    writer.writerow(["Company Report", name])
    writer.writerow(["Matched Devices", company.get("matched_count", 0)])
    writer.writerow(["Mismatched Devices", company.get("mismatched_count", 0)])
    writer.writerow([])

    # Device table
    writer.writerow([
        "Hostname",
        "Endpoint Protection",
        "Match Type",
        "Platforms",
        "In_PSA",
        "In_S1",
        "In_SC",
        "In_ASIO",
        "Endpoint ID",
        "MAC Address",
        "Serial Number",
        "Site",
    ])

    def _write_device(dev: dict, match_type: str) -> None:
        platforms = dev.get("platforms", [])
        plat_str = " | ".join(platforms)
        writer.writerow([
            dev.get("hostname", ""),
            dev.get("endpoint_protection", ""),
            match_type,
            plat_str,
            "Yes" if "connectwise_psa" in platforms else "No",
            "Yes" if "sentinelone" in platforms else "No",
            "Yes" if "screenconnect" in platforms else "No",
            "Yes" if "asio" in platforms else "No",
            dev.get("endpoint_id", ""),
            dev.get("mac_address", ""),
            dev.get("serial_number", ""),
            dev.get("site_name", ""),
        ])

    for dev in company.get("matched", []):
        _write_device(dev, dev.get("match_type", "matched"))

    for plat, devs in company.get("mismatched", {}).items():
        for dev in devs:
            _write_device(dev, "unmatched")

    buf.seek(0)
    safe_name = "".join(c if c.isalnum() or c in (" ", "-", "_") else "_" for c in name)[:50]
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}_{timestamp}.csv"'
        },
    )


# ── AV compliance report (machines missing assigned AV) ──────────────────────
# These routes must stay ABOVE the ``/{match_id}`` catch-all below so the
# ``av-compliance`` path is not swallowed as a match id.

_AV_VENDOR_LABELS: dict[str, str] = {
    "eset": "ESET",
    "sentinelone": "SentinelOne",
}


async def _av_compliance_report(
    vendor: str, session: AsyncSession,
) -> dict[str, Any]:
    """Build the AV-compliance report for *vendor* from reconciled data.

    Validates the vendor, loads reconciled data (cache first, then a fresh
    reconciliation if the cache is empty) and projects it via
    ``_build_av_compliance``.
    """
    vendor = (vendor or "").lower().strip()
    if vendor not in _AV_ASSIGNED_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported vendor '{vendor}'. "
                "Use 'eset' or 'sentinelone'."
            ),
        )

    data = _reconciled_cache.get("data")
    scan_required = False
    if not data:
        data = await company_reconciled(session=session, force=True)
        scan_required = not data

    report = _build_av_compliance(data or [], vendor)
    report["scan_required"] = scan_required
    return report


@router.get(
    "/av-compliance/{vendor}",
    response_model=dict[str, Any],
    summary="Machines missing the AV assigned by PSA agreement (eset|sentinelone)",
)
async def av_compliance(
    vendor: str,
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Report companies contracted for an AV and their Asio machines missing it.

    ``vendor`` is ``eset`` or ``sentinelone``.  A company is included when its
    PSA agreement has an active addition for that AV (``assigned_av`` is the
    vendor or ``both``).  For each such company the response lists the Asio
    endpoints that do **not** have the AV installed — i.e. the machines that
    still need it.  ESET is the primary use case (failing install plugin);
    SentinelOne reuses the identical structure.

    Data comes from the reconciled inventory populated by
    ``POST /api/companies/scan``.  If no reconciled data exists yet the
    response sets ``scan_required: true`` and returns empty results.
    """
    return await _av_compliance_report(vendor, session)


@router.get("/av-compliance/{vendor}/export")
async def export_av_compliance_csv(
    vendor: str,
    session: AsyncSession = Depends(_get_session),
) -> StreamingResponse:
    """CSV download: one row per machine missing the assigned AV."""
    report = await _av_compliance_report(vendor, session)
    label = _AV_VENDOR_LABELS.get(report["vendor"], report["vendor"].upper())

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Company",
        "Assigned AV",
        "Hostname",
        f"{label} Status",
        "Detected AV",
        "Gap Reason",
        "Endpoint ID",
        "Site",
        "Platforms",
        "Match Type",
    ])

    for company in report["companies"]:
        name = company.get("company", "")
        assigned = company.get("assigned_av", "")
        for dev in company.get("missing_devices", []):
            writer.writerow([
                name,
                assigned,
                dev.get("hostname", ""),
                f"Missing {label}",
                dev.get("detected_av", "None"),
                dev.get("gap_reason", ""),
                dev.get("endpoint_id", ""),
                dev.get("site_name", ""),
                " | ".join(dev.get("platforms", [])),
                dev.get("match_type", ""),
            ])

    buf.seek(0)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="missing_{report["vendor"]}_{timestamp}.csv"'
            )
        },
    )


@router.get("/av-compliance/{vendor}/export.xls")
async def export_av_compliance_excel(
    vendor: str,
    session: AsyncSession = Depends(_get_session),
) -> StreamingResponse:
    """Excel-readable (SpreadsheetML) export of machines missing the AV."""
    report = await _av_compliance_report(vendor, session)
    label = _AV_VENDOR_LABELS.get(report["vendor"], report["vendor"].upper())

    def _cell(value: Any, style: str = "") -> str:
        attr = f' style="{style}"' if style else ""
        return f"<td{attr}>{html.escape(str(value or ''))}</td>"

    company_style = "background:#dbeafe;color:#1e3a8a;font-weight:bold;"
    missing_style = "background:#fee2e2;color:#7f1d1d;font-weight:bold;"
    other_av_style = "background:#fef3c7;color:#78350f;font-weight:bold;"
    header_style = "background:#1e293b;color:#ffffff;font-weight:bold;"

    totals = report["totals"]
    parts = [
        "<html><head><meta charset='utf-8'>",
        "<style>table{border-collapse:collapse;font-family:Segoe UI,Arial,sans-serif;"
        "font-size:12px;}td,th{border:1px solid #cbd5e1;padding:5px 8px;} "
        ".spacer td{border:none;height:12px;}</style>",
        "</head><body>",
        f"<h2>Machines missing {html.escape(label)}</h2>",
        f"<p>Companies: {totals['companies']} &nbsp; "
        f"Asio devices: {totals['asio_devices']} &nbsp; "
        f"Compliant: {totals['compliant']} &nbsp; "
        f"Missing: {totals['missing']} &nbsp; "
        f"(No AV detected: {totals.get('missing_no_av', 0)} &nbsp; "
        f"Different AV detected: {totals.get('missing_other_av', 0)})</p>",
        f"<p style='font-size:11px;color:#475569;'>Rows shaded red = no AV "
        f"detected at all; rows shaded amber = a different AV was detected "
        f"(not {html.escape(label)}). 'Missing' means {html.escape(label)} was "
        f"not found in the Asio application scan or the {html.escape(label)} "
        f"cloud portal for that machine.</p>",
        "<table>",
    ]

    for company in report["companies"]:
        name = company.get("company", "")
        parts.append(
            "<tr>"
            + _cell(
                f"{name}  —  {company.get('summary', '')}",
                company_style,
            )
            + "</tr>"
        )
        missing_devices = company.get("missing_devices", [])
        if not missing_devices:
            continue
        parts.append(
            "<tr>"
            + "".join(_cell(h, header_style) for h in [
                "Hostname", "Detected AV", "Gap Reason",
                "Endpoint ID", "Site", "Platforms", "Match Type",
            ])
            + "</tr>"
        )
        for dev in missing_devices:
            detected = dev.get("detected_av", "None")
            row_style = other_av_style if detected and detected != "None" else missing_style
            parts.append("<tr>")
            parts.append(_cell(dev.get("hostname", ""), row_style))
            parts.append(_cell(detected, row_style))
            parts.append(_cell(dev.get("gap_reason", "")))
            parts.append(_cell(dev.get("endpoint_id", "")))
            parts.append(_cell(dev.get("site_name", "")))
            parts.append(_cell(" | ".join(dev.get("platforms", []))))
            parts.append(_cell(dev.get("match_type", "")))
            parts.append("</tr>")
        parts.append("<tr class='spacer'><td colspan='7'></td></tr>")

    parts.append("</table></body></html>")
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter(["".join(parts)]),
        media_type="application/vnd.ms-excel",
        headers={
            "Content-Disposition": (
                f'attachment; filename="missing_{report["vendor"]}_{timestamp}.xls"'
            )
        },
    )


@router.get("/{match_id}", response_model=dict[str, Any])
async def get_company(
    match_id: str, session: AsyncSession = Depends(_get_session)
) -> dict[str, Any]:
    """Return a single company match by ID."""
    repo = ReconciliationRepository(session)
    matches = await repo.list_company_matches()
    for m in matches:
        if m.id == match_id:
            return {
                "id": m.id,
                "platform_a": m.platform_a,
                "platform_id_a": m.platform_id_a,
                "name_a": m.name_a,
                "platform_b": m.platform_b,
                "platform_id_b": m.platform_id_b,
                "name_b": m.name_b,
                "score": m.score,
                "status": m.status,
            }
    raise HTTPException(status_code=404, detail="Company match not found")


# ── Device Cleanup Actions ────────────────────────────────────────


class MarkInactiveRequest(BaseModel):
    """Request to mark a PSA device as inactive."""
    platform_id: str
    reason: str | None = None
    confirm_token: str | None = None


class RemoveDeviceRequest(BaseModel):
    """Request to remove a device from SentinelOne."""
    endpoint_id: str
    reason: str | None = None
    confirm_token: str | None = None


@router.post(
    "/devices/psa/mark-inactive",
    response_model=dict[str, Any],
    summary="[WRITE] Mark a PSA configuration inactive (breakdown-page cleanup). Requires confirmation.",
    openapi_extra={"x-destructive": True},
)
async def mark_psa_device_inactive(
    body: MarkInactiveRequest,
    request: Request,
) -> dict[str, Any]:
    """Mark a PSA configuration as inactive.

    This is useful for cleaning up PSA-only devices that are no longer in use
    but still appear in the reconciliation as mismatched.
    """
    await enforce_direct_write("connectwise_psa")
    enforce_confirmation(
        request,
        action="psa.mark_inactive",
        target=body.platform_id,
        confirm_token=body.confirm_token,
        preview=build_preview(
            "psa.mark_inactive",
            body.platform_id,
            platform="connectwise_psa",
            effect="Sets activeFlag=false on the ConnectWise PSA configuration.",
            reversible=True,
            reason=body.reason,
        ),
    )

    psa = _get_psa_connector()
    if not psa:
        raise HTTPException(status_code=503, detail="PSA connector not available")

    try:
        from app.services.cwm_gateway import api_request

        # Fetch current config to read existing notes
        config = await api_request(
            "GET", f"/company/configurations/{body.platform_id}"
        )

        # Build patch operations array — CW Manage uses application/json
        patch_ops: list[dict[str, Any]] = [
            {"op": "replace", "path": "activeFlag", "value": False},
        ]
        if body.reason:
            notes = config.get("notes", "") or ""
            if notes:
                notes += "\n\n"
            notes += f"Marked inactive via MSP Tools: {body.reason}"
            patch_ops.append(
                {"op": "replace", "path": "notes", "value": notes},
            )

        await api_request(
            "PATCH", f"/company/configurations/{body.platform_id}", data=patch_ops
        )

        log.info(
            "psa.mark_inactive.success",
            platform_id=body.platform_id,
            reason=body.reason,
        )

        return {
            "status": "success",
            "platform": "connectwise_psa",
            "platform_id": body.platform_id,
            "action": "marked_inactive",
        }

    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error(
            "psa.mark_inactive.exception",
            platform_id=body.platform_id,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=str(exc))


@router.post(
    "/devices/sentinelone/remove",
    response_model=dict[str, Any],
    summary="[WRITE] Decommission and remove a SentinelOne agent. Requires confirmation.",
    openapi_extra={"x-destructive": True},
)
async def remove_sentinelone_device(
    body: RemoveDeviceRequest,
    request: Request,
) -> dict[str, Any]:
    """Remove a device from SentinelOne.

    This decommissions the agent and removes it from the console.
    Useful for cleaning up S1-only devices that are no longer managed.
    """
    await enforce_direct_write("sentinelone")
    enforce_confirmation(
        request,
        action="sentinelone.remove",
        target=body.endpoint_id,
        confirm_token=body.confirm_token,
        preview=build_preview(
            "sentinelone.remove",
            body.endpoint_id,
            platform="sentinelone",
            effect="Decommissions the SentinelOne agent and removes it from the console.",
            reversible=False,
            reason=body.reason,
        ),
    )

    connectors = get_connectors()
    s1_connector = None
    for c in connectors:
        if c.platform_name == "sentinelone":
            s1_connector = c
            break

    if not s1_connector:
        raise HTTPException(status_code=503, detail="SentinelOne connector not available")

    try:
        import httpx

        # SentinelOne uses agent ID, need to look it up from endpoint_id (UUID)
        # First, get the agent by filtering
        base_url = s1_connector._settings.s1_base_url.rstrip("/")
        headers = {"Authorization": f"ApiToken {s1_connector._settings.s1_api_token}"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Search for agent by ID — platform_id is the S1 numeric agent ID
            search_url = f"{base_url}/web/api/v2.1/agents"
            search_resp = await client.get(
                search_url,
                headers=headers,
                params={"ids": body.endpoint_id, "limit": 1},
            )
            search_resp.raise_for_status()
            search_data = search_resp.json()

            agents = search_data.get("data", [])
            if not agents:
                raise HTTPException(
                    status_code=404,
                    detail=f"No SentinelOne agent found with ID {body.endpoint_id}",
                )

            agent_id = agents[0].get("id")
            hostname = agents[0].get("computerName", "unknown")

            # Decommission the agent
            decommission_url = f"{base_url}/web/api/v2.1/agents/actions/decommission"
            decom_resp = await client.post(
                decommission_url,
                headers=headers,
                json={"filter": {"ids": [agent_id]}},
            )
            decom_resp.raise_for_status()

        log.info(
            "sentinelone.remove.success",
            endpoint_id=body.endpoint_id,
            agent_id=agent_id,
            hostname=hostname,
            reason=body.reason,
        )

        return {
            "status": "success",
            "platform": "sentinelone",
            "endpoint_id": body.endpoint_id,
            "agent_id": agent_id,
            "hostname": hostname,
            "action": "decommissioned",
        }

    except httpx.HTTPStatusError as exc:
        log.error(
            "sentinelone.remove.error",
            endpoint_id=body.endpoint_id,
            status=exc.response.status_code,
            error=exc.response.text[:300],
        )
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"SentinelOne API error: {exc.response.text[:200]}",
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "sentinelone.remove.exception",
            endpoint_id=body.endpoint_id,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=str(exc))


@router.post(
    "/devices/asio/remove",
    response_model=dict[str, Any],
    summary="[WRITE] Delete an endpoint from Asio. Requires confirmation.",
    openapi_extra={"x-destructive": True},
)
async def remove_asio_device(
    body: RemoveDeviceRequest,
    request: Request,
) -> dict[str, Any]:
    """Remove (delete) an endpoint from Asio.

    Used to clean up devices that exist in Asio but no longer have a
    ScreenConnect session and have not communicated recently (past 45 days),
    so they are treated as decommissioned.
    """
    await enforce_direct_write("asio")
    enforce_confirmation(
        request,
        action="asio.remove",
        target=body.endpoint_id,
        confirm_token=body.confirm_token,
        preview=build_preview(
            "asio.remove",
            body.endpoint_id,
            platform="asio",
            effect="Deletes the endpoint/agent from Asio (RMM).",
            reversible=False,
            reason=body.reason,
        ),
    )

    connectors = get_connectors()
    asio_connector = None
    for c in connectors:
        if c.platform_name == "asio":
            asio_connector = c
            break

    if not asio_connector:
        raise HTTPException(status_code=503, detail="Asio connector not available")

    try:
        await asio_connector.authenticate()
        result = await asio_connector.remove_agent(
            body.endpoint_id, reason=body.reason,
        )

        if result and result.get("error"):
            raise HTTPException(status_code=502, detail=result["error"])

        log.info(
            "asio.remove.success",
            endpoint_id=body.endpoint_id,
            reason=body.reason,
        )
        return result or {
            "status": "success",
            "platform": "asio",
            "endpoint_id": body.endpoint_id,
            "action": "agent_deleted",
        }

    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error(
            "asio.remove.exception",
            endpoint_id=body.endpoint_id,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=str(exc))
