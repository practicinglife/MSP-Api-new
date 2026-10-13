"""Endpoint-ID-first device identity resolution and DB persistence.

This module replaces fuzzy company-name matching with a deterministic
approach:

1. **Endpoint ID matching** — ASIO endpoint_id is the golden key.  PSA
   stores the same UUID in a custom field.  When two devices share an
   endpoint_id, they are the *same physical machine* regardless of what
   the company name or hostname says.

2. **Hostname matching** — ScreenConnect and SentinelOne do not carry
   endpoint_id.  For these platforms, normalised hostname within the
   same canonical company is the fallback join key.

3. **Company resolution** — normalisation + alias map (DB aliases,
   fuzzy auto-aliases, manual overrides) is applied to every device's
   raw company name to produce a canonical name.  This is persisted in
   ``company_mappings`` so it only needs to be recomputed on scan.

4. **Change detection** — on each scan the incoming endpoint_id set is
   compared against the stored set.  Additions, removals, and company
   moves are reported so remediation scripts know what changed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.models.device import DeviceRecord
from app.services.normalizers import hostname_match_key, normalize_company_name, normalize_hostname

log = structlog.get_logger(__name__)

_SC_NETBIOS_LIMIT = 15
_MAC_BLOCKLIST: frozenset[str] = frozenset({"", "000000000000", "FFFFFFFFFFFF"})


def prepare_device_rows(
    platform_devices: list[tuple[str, list[DeviceRecord]]],
    alias_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Convert live connector device records into DB-ready dicts.

    Applies company normalisation + alias resolution and returns a flat
    list of dicts suitable for ``PlatformDeviceRepository.bulk_upsert()``.
    """
    rows: list[dict[str, Any]] = []
    for platform, devices in platform_devices:
        for dev in devices:
            company_raw = dev.company_name or dev.company_platform_id or ""
            norm = normalize_company_name(company_raw)
            canonical = alias_map.get(norm, norm) if norm else ""

            rows.append({
                "platform": platform,
                "platform_id": dev.platform_id,
                "endpoint_id": dev.endpoint_id or "",
                "hostname": dev.hostname or "",
                "hostname_norm": normalize_hostname(dev.hostname) if dev.hostname else "",
                "company_raw": company_raw,
                "company_canonical": canonical,
                "company_platform_id": str(dev.company_platform_id) if dev.company_platform_id else "",
                "site_name": dev.site_name or "",
                "serial_number": dev.serial_number or "",
                "mac_addresses": ",".join(dev.mac_addresses) if dev.mac_addresses else "",
                "raw_json": dev.raw if dev.raw else None,
            })
    return rows


def prepare_company_mappings(
    platform_devices: list[tuple[str, list[DeviceRecord]]],
    alias_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Build company mapping rows from device data.

    Returns a list of dicts suitable for
    ``CompanyMappingRepository.bulk_upsert()``.
    """
    seen: set[tuple[str, str]] = set()
    mappings: list[dict[str, Any]] = []

    for platform, devices in platform_devices:
        for dev in devices:
            raw = dev.company_name or dev.company_platform_id or ""
            if not raw:
                continue
            key = (platform, raw)
            if key in seen:
                continue
            seen.add(key)

            norm = normalize_company_name(raw)
            canonical = alias_map.get(norm, norm) if norm else ""
            # Determine alias source
            source = "manual" if norm in alias_map and alias_map[norm] != norm else "auto"

            mappings.append({
                "platform": platform,
                "raw_name": raw,
                "normalized_name": norm,
                "canonical_name": canonical,
                "alias_source": source,
            })
    return mappings


def detect_endpoint_changes(
    incoming: dict[str, str],
    stored: dict[str, str],
) -> dict[str, Any]:
    """Compare incoming endpoint_id→company map against stored version.

    Parameters
    ----------
    incoming : dict[str, str]
        {endpoint_id: company_canonical} from the current live scan.
    stored : dict[str, str]
        {endpoint_id: company_canonical} from the database.

    Returns
    -------
    dict with keys:
        added:   list of endpoint_ids new in this scan
        removed: list of endpoint_ids no longer present
        moved:   list of {endpoint_id, old_company, new_company}
        unchanged: count of endpoint_ids with no change
    """
    incoming_ids = set(incoming.keys())
    stored_ids = set(stored.keys())

    added = sorted(incoming_ids - stored_ids)
    removed = sorted(stored_ids - incoming_ids)
    moved: list[dict[str, str]] = []
    unchanged = 0

    for eid in incoming_ids & stored_ids:
        if incoming[eid] != stored[eid]:
            moved.append({
                "endpoint_id": eid,
                "old_company": stored[eid],
                "new_company": incoming[eid],
            })
        else:
            unchanged += 1

    return {
        "added": added,
        "removed": removed,
        "moved": moved,
        "unchanged": unchanged,
        "total_incoming": len(incoming),
        "total_stored": len(stored),
    }


def reconcile_from_db(
    all_devices: list[Any],
    av_lookup: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Build per-company reconciled view from stored PlatformDevice records.

    Uses endpoint_id as primary join key (ASIO ↔ PSA), then falls back to
    hostname_norm for ScreenConnect / SentinelOne.

    Parameters
    ----------
    all_devices : list[PlatformDevice]
        All rows from platform_devices table.
    av_lookup : dict, optional
        {hostname_norm: [{av_vendor, av_product, av_status, ...}]} from
        ``DeviceAvStatusRepository.get_av_lookup()``.  When provided, each
        reconciled device is enriched with per-device AV detection data.

    Returns
    -------
    List of per-company dicts with matched / mismatched device lists.
    """
    # Group by canonical company
    company_devices: dict[str, list[Any]] = {}
    for dev in all_devices:
        company_devices.setdefault(dev.company_canonical, []).append(dev)

    results: list[dict[str, Any]] = []

    for company in sorted(company_devices.keys()):
        if not company:
            continue
        devs = company_devices[company]

        # Phase 1: endpoint_id-based matching
        # Group by endpoint_id (non-empty)
        eid_groups: dict[str, dict[str, Any]] = {}
        no_eid: list[Any] = []

        for dev in devs:
            if dev.endpoint_id:
                group = eid_groups.setdefault(dev.endpoint_id, {
                    "endpoint_id": dev.endpoint_id,
                    "platforms": {},
                })
                group["platforms"][dev.platform] = dev
            else:
                no_eid.append(dev)

        # Phase 2: hostname-based matching for devices without endpoint_id
        # Try to merge no_eid devices into existing eid_groups by hostname
        # Uses skeleton key (strips punctuation/smart-quotes) + prefix fallback
        # for ScreenConnect 15-char NetBIOS truncation.
        skel_to_eid: dict[str, str] = {}
        for eid, group in eid_groups.items():
            for plat, dev in group["platforms"].items():
                if dev.hostname_norm:
                    skel = hostname_match_key(dev.hostname_norm)
                    if skel:
                        skel_to_eid[skel] = eid

        still_unmatched: list[Any] = []
        for dev in no_eid:
            if not dev.hostname_norm:
                still_unmatched.append(dev)
                continue
            skel = hostname_match_key(dev.hostname_norm)
            if skel and skel in skel_to_eid:
                eid = skel_to_eid[skel]
                eid_groups[eid]["platforms"][dev.platform] = dev
                continue
            # Prefix fallback for 15-char truncated hostnames
            matched_eid = None
            if skel and len(dev.hostname_norm) == 15:
                for existing_skel, eid in skel_to_eid.items():
                    if existing_skel.startswith(skel) and dev.platform not in eid_groups[eid]["platforms"]:
                        matched_eid = eid
                        break
            if not matched_eid and skel:
                for existing_skel, eid in skel_to_eid.items():
                    if skel.startswith(existing_skel) and dev.platform not in eid_groups[eid]["platforms"]:
                        matched_eid = eid
                        break
            if matched_eid:
                eid_groups[matched_eid]["platforms"][dev.platform] = dev
            else:
                still_unmatched.append(dev)

        # Phase 2b: MAC fallback against eid_groups — try to merge devices
        # that didn't match by hostname/prefix into eid groups by shared MAC.
        eid_mac_index: dict[str, str] = {}
        for eid, group in eid_groups.items():
            for dev in group["platforms"].values():
                raw_macs = getattr(dev, "mac_addresses", "") or ""
                for mac in (m.strip() for m in raw_macs.split(",") if m.strip()):
                    if mac and mac not in _MAC_BLOCKLIST:
                        eid_mac_index[mac] = eid

        remaining: list[Any] = []
        for dev in still_unmatched:
            raw_macs = getattr(dev, "mac_addresses", "") or ""
            dev_macs = [m.strip() for m in raw_macs.split(",") if m.strip()]
            matched_eid = None
            for mac in dev_macs:
                if mac in _MAC_BLOCKLIST:
                    continue
                if mac in eid_mac_index:
                    eid = eid_mac_index[mac]
                    if dev.platform not in eid_groups[eid]["platforms"]:
                        matched_eid = eid
                        break
            if matched_eid:
                eid_groups[matched_eid]["platforms"][dev.platform] = dev
            else:
                remaining.append(dev)
        still_unmatched = remaining

        # Phase 3: group remaining unmatched by skeleton key
        hostname_groups: dict[str, dict[str, Any]] = {}
        display_hosts: dict[str, str] = {}  # skel → best display hostname
        for dev in still_unmatched:
            if dev.hostname_norm:
                skel = hostname_match_key(dev.hostname_norm)
                if not skel:
                    continue
                group = hostname_groups.setdefault(skel, {
                    "endpoint_id": "",
                    "platforms": {},
                })
                group["platforms"][dev.platform] = dev
                prev = display_hosts.get(skel, "")
                if len(dev.hostname_norm) > len(prev):
                    display_hosts[skel] = dev.hostname_norm

        # Phase 3b: prefix merge for 15-char truncated hostnames
        _db_prefix_merge(hostname_groups, display_hosts)

        # Phase 3c: MAC fallback merge — merge single-platform groups sharing a MAC
        _db_mac_merge(hostname_groups, display_hosts)

        # Merge all groups and classify
        all_groups = list(eid_groups.values()) + list(hostname_groups.values())

        # Determine all platforms active for this company — "full" means a
        # device is present on every platform the company uses, not just ≥3.
        all_company_platforms: set[str] = set()
        for group in all_groups:
            all_company_platforms.update(group["platforms"].keys())
        total_company_platforms = len(all_company_platforms)

        matched: list[dict[str, Any]] = []
        mismatched: dict[str, list[dict[str, Any]]] = {}

        for group in all_groups:
            platforms = group["platforms"]
            platform_names = sorted(platforms.keys())

            # Pick best data across platforms
            best = _merge_best(group["endpoint_id"], platforms)
            best["platforms"] = platform_names

            # Enrich with per-device AV status from detection sources
            if av_lookup:
                _enrich_av_status(best, platforms, av_lookup)

            if len(platform_names) >= 2:
                best["match_type"] = (
                    "full"
                    if len(platform_names) >= total_company_platforms
                    else "partial"
                )
                matched.append(best)
            else:
                best["match_type"] = "unmatched"
                plat = platform_names[0]
                mismatched.setdefault(plat, []).append(best)

        mismatch_total = sum(len(v) for v in mismatched.values())
        results.append({
            "company": company,
            "matched": matched,
            "mismatched": mismatched,
            "matched_count": len(matched),
            "mismatched_count": mismatch_total,
        })

    log.info(
        "reconcile_from_db.done",
        companies=len(results),
        total_matched=sum(r["matched_count"] for r in results),
        total_mismatched=sum(r["mismatched_count"] for r in results),
    )
    return results


def _enrich_av_status(
    device: dict[str, Any],
    platforms: dict[str, Any],
    av_lookup: dict[str, list[dict[str, Any]]],
) -> None:
    """Enrich a reconciled device dict with per-device AV detection data.

    Looks up the device's hostname_norm in the av_lookup table and merges
    any detected AV products into the device dict.
    """
    # Collect all hostname_norms for this device group
    hostname_norms: set[str] = set()
    for dev in platforms.values():
        if dev.hostname_norm:
            hostname_norms.add(dev.hostname_norm)

    # Merge AV detections from all matching hostnames
    detected_av: list[dict[str, Any]] = []
    seen_vendors: set[str] = set()
    for hn in hostname_norms:
        for av_entry in av_lookup.get(hn, []):
            vendor = av_entry.get("av_vendor", "")
            if vendor and vendor not in seen_vendors:
                seen_vendors.add(vendor)
                detected_av.append(av_entry)

    device["av_detections"] = detected_av
    device["av_vendors"] = sorted(seen_vendors)

    # Determine ESET status specifically — prefer the Endpoint Security /
    # Server Security product over the Management Agent for version display.
    eset_entries = [a for a in detected_av if a.get("av_vendor") == "eset"]
    if eset_entries:
        # Pick best entry: prioritise Endpoint/Server Security over Management Agent
        best_eset = eset_entries[0]
        for e in eset_entries:
            prod_lower = (e.get("av_product") or "").lower()
            if "endpoint" in prod_lower or "security" in prod_lower or "server" in prod_lower:
                best_eset = e
                break
        device["eset_installed"] = True
        device["eset_product"] = best_eset.get("av_product", "")
        device["eset_version"] = best_eset.get("av_version", "")
        device["eset_enabled"] = best_eset.get("av_enabled")
        device["eset_up_to_date"] = best_eset.get("av_up_to_date")
    else:
        device["eset_installed"] = False
        device["eset_product"] = ""
        device["eset_version"] = ""
        device["eset_enabled"] = None
        device["eset_up_to_date"] = None

    # Determine SentinelOne version — prefer raw_json agentVersion from
    # the platform_devices record, fall back to device_av_status entries.
    s1_dev = platforms.get("sentinelone")
    s1_agent_version = ""
    if s1_dev and hasattr(s1_dev, "raw_json") and s1_dev.raw_json:
        s1_agent_version = s1_dev.raw_json.get("agentVersion", "")
    if not s1_agent_version:
        s1_av = [a for a in detected_av if a.get("av_vendor") == "sentinelone"]
        if s1_av:
            s1_agent_version = s1_av[0].get("av_version", "")
    device["s1_installed"] = "sentinelone" in [p for p in platforms]
    device["s1_agent_version"] = s1_agent_version


def _merge_best(
    endpoint_id: str,
    platforms: dict[str, Any],
) -> dict[str, Any]:
    """Merge best data from all platform records for one physical device.

    Priority: ASIO > PSA > SentinelOne > ScreenConnect for most fields.
    """
    hostname = ""
    mac_address = ""
    serial_number = ""
    site_name = ""
    platform_id = ""
    company_platform_id = ""

    for plat in ("asio", "connectwise_psa", "sentinelone", "screenconnect"):
        if plat not in platforms:
            continue
        dev = platforms[plat]
        if not hostname and dev.hostname:
            hostname = dev.hostname
        if not mac_address and dev.mac_addresses:
            mac_address = dev.mac_addresses
        if not serial_number and dev.serial_number:
            serial_number = dev.serial_number
        if not site_name and dev.site_name:
            site_name = dev.site_name
        if not platform_id and dev.platform_id:
            platform_id = dev.platform_id
        # Prefer ASIO company_platform_id for the actual company ID in remediations
        if not company_platform_id and hasattr(dev, 'company_platform_id') and dev.company_platform_id:
            company_platform_id = str(dev.company_platform_id)
        # Prefer ASIO endpoint_id
        if not endpoint_id and dev.endpoint_id:
            endpoint_id = dev.endpoint_id

    # Extract SentinelOne agent version from raw_json when available
    s1_agent_version = ""
    if "sentinelone" in platforms:
        s1_raw = platforms["sentinelone"].raw_json
        if s1_raw:
            s1_agent_version = s1_raw.get("agentVersion", "")

    return {
        "hostname": hostname,
        "endpoint_id": endpoint_id,
        "platform_id": platform_id,
        "mac_address": mac_address,
        "serial_number": serial_number,
        "site_name": site_name,
        "company_platform_id": company_platform_id,
        "s1_agent_version": s1_agent_version,
    }


# ── Prefix merge for DB-backed reconciliation ────────────────────


def _db_prefix_merge(
    hostname_groups: dict[str, dict[str, Any]],
    display_hosts: dict[str, str],
) -> None:
    """Merge skeleton-key groups where a truncated hostname is a prefix.

    Same logic as ``reconciler._prefix_merge`` but operates on the
    ``{skel: {"endpoint_id": ..., "platforms": {...}}}`` dicts produced
    by :func:`reconcile_from_db`.
    """
    merged_into: dict[str, str] = {}
    singles = [
        skel for skel, grp in hostname_groups.items()
        if len(grp["platforms"]) == 1
    ]
    singles.sort(key=len)

    for short_skel in singles:
        if short_skel in merged_into:
            continue
        orig = display_hosts.get(short_skel, "")
        if len(orig) != _SC_NETBIOS_LIMIT:
            continue
        for long_skel in sorted(hostname_groups.keys(), key=len, reverse=True):
            if long_skel == short_skel or long_skel in merged_into:
                continue
            if len(long_skel) <= len(short_skel):
                continue
            if not long_skel.startswith(short_skel):
                continue
            if set(hostname_groups[short_skel]["platforms"]) & set(hostname_groups[long_skel]["platforms"]):
                continue
            for plat, dev in hostname_groups[short_skel]["platforms"].items():
                hostname_groups[long_skel]["platforms"][plat] = dev
            merged_into[short_skel] = long_skel
            log.debug(
                "reconcile_from_db.prefix_merge",
                short=orig,
                long=display_hosts.get(long_skel, long_skel),
            )
            break

    for skel in merged_into:
        del hostname_groups[skel]
        display_hosts.pop(skel, None)


def _db_mac_merge(
    hostname_groups: dict[str, dict[str, Any]],
    display_hosts: dict[str, str],
) -> None:
    """Merge single-platform groups that share a physical MAC address.

    ``mac_addresses`` on DB records is a comma-separated string; this
    helper splits it before comparison.
    """
    # Build MAC → skeleton reverse index for all entries
    mac_to_skels: dict[str, list[str]] = {}
    for skel, grp in hostname_groups.items():
        for dev in grp["platforms"].values():
            raw_macs = getattr(dev, "mac_addresses", "") or ""
            macs = [m.strip() for m in raw_macs.split(",") if m.strip()]
            for mac in macs:
                if mac and mac not in _MAC_BLOCKLIST:
                    mac_to_skels.setdefault(mac, []).append(skel)

    merged_into: dict[str, str] = {}

    for skel in list(hostname_groups.keys()):
        if skel in merged_into or len(hostname_groups[skel]["platforms"]) != 1:
            continue
        dev = next(iter(hostname_groups[skel]["platforms"].values()))
        raw_macs = getattr(dev, "mac_addresses", "") or ""
        macs = [m.strip() for m in raw_macs.split(",") if m.strip()]
        for mac in macs:
            if mac in _MAC_BLOCKLIST:
                continue
            for target_skel in mac_to_skels.get(mac, []):
                if target_skel == skel or target_skel in merged_into:
                    continue
                if target_skel not in hostname_groups:
                    continue
                if set(hostname_groups[skel]["platforms"]) & set(hostname_groups[target_skel]["platforms"]):
                    continue
                for plat, d in hostname_groups[skel]["platforms"].items():
                    if plat not in hostname_groups[target_skel]["platforms"]:
                        hostname_groups[target_skel]["platforms"][plat] = d
                if len(display_hosts.get(skel, "")) > len(display_hosts.get(target_skel, "")):
                    display_hosts[target_skel] = display_hosts[skel]
                merged_into[skel] = target_skel
                log.debug(
                    "reconcile_from_db.mac_merge",
                    mac=mac,
                    merged=display_hosts.get(skel, skel),
                    into=display_hosts.get(target_skel, target_skel),
                )
                break
            if skel in merged_into:
                break

    for skel in merged_into:
        hostname_groups.pop(skel, None)
        display_hosts.pop(skel, None)
