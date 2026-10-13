"""Cross-platform device reconciliation service.

Matches devices across Asio, ConnectWise PSA, ScreenConnect, and
SentinelOne using a multi-tier strategy:

1. **Endpoint ID** — ASIO and PSA share a UUID endpoint_id; devices with
   the same endpoint_id are the same physical machine.  Devices without
   endpoint_id (ScreenConnect, SentinelOne) are merged into existing
   endpoint_id groups by hostname.
2. **Skeleton hostname** — Normalised, punctuation-stripped hostname key.
3. **Prefix merge** — 15-char NetBIOS truncation fallback (ScreenConnect).
4. **MAC address** — Last-resort fallback for remaining unmatched devices.

Each device is classified as "full" (present on all company platforms),
"partial" (2+ but not all), or "unmatched" (1 platform only).

ESET is handled at the *company* level only (no device endpoint in EMA2),
so it does not participate in device matching.

PSA duplicates (same hostname + endpoint_id) are collapsed before matching.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.models.device import DeviceRecord
from app.services.normalizers import hostname_match_key, normalize_company_name

log = structlog.get_logger(__name__)


def reconcile_devices(
    platform_devices: list[tuple[str, list[DeviceRecord]]],
    alias_map: dict[str, str] | None = None,
    prefix_canonical: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Run cross-platform reconciliation and return per-company results.

    Parameters
    ----------
    platform_devices:
        List of (platform_name, devices) tuples from each connector.
    alias_map:
        Optional alias_name → canonical_name mapping.
    prefix_canonical:
        Optional original_name → canonical_name from prefix dedup.

    Returns
    -------
    List of dicts, one per company::

        {
            "company": "accurate heating cooling",
            "matched": [ {hostname, endpoint_id, mac_address, serial_number,
                          site_name, platforms: ["asio","connectwise_psa","screenconnect"],
                          match_type: "full"}, ... ],
            "mismatched": {
                "asio": [ {hostname, endpoint_id, ...}, ... ],
                "connectwise_psa": [...],
                "screenconnect": [...],
            },
            "matched_count": 6,
            "mismatched_count": 1,
        }
    """
    alias_map = alias_map or {}
    prefix_canonical = prefix_canonical or {}

    # ── Step 1: Group devices by normalised company ──────────────
    # company → platform → list[DeviceRecord]
    company_platform: dict[str, dict[str, list[DeviceRecord]]] = {}

    for platform, devices in platform_devices:
        for dev in devices:
            company = dev.company_name or dev.company_platform_id or ""
            if not company:
                continue
            company = normalize_company_name(company)
            if not company:
                continue
            # Apply alias then prefix canonical
            company = alias_map.get(company, company)
            company = prefix_canonical.get(company, company)
            if not company:
                continue
            company_platform.setdefault(company, {})
            company_platform[company].setdefault(platform, [])
            company_platform[company][platform].append(dev)

    # ── Step 2: Per-company reconciliation ───────────────────────
    results: list[dict[str, Any]] = []

    for company in sorted(company_platform.keys()):
        plat_devs = company_platform[company]

        # 2a. PSA dedup — collapse entries with identical (hostname, endpoint_id)
        if "connectwise_psa" in plat_devs:
            plat_devs["connectwise_psa"] = _dedup_psa(plat_devs["connectwise_psa"])

        # 2b. Endpoint-ID matching (golden key: ASIO ↔ PSA)
        #     Devices sharing the same non-empty endpoint_id are the same
        #     physical machine regardless of hostname differences.
        eid_groups: dict[str, dict[str, DeviceRecord]] = {}
        eid_display: dict[str, str] = {}  # eid → best display hostname
        no_eid: list[tuple[str, DeviceRecord]] = []  # (platform, dev)

        for platform, devs in plat_devs.items():
            for dev in devs:
                if dev.endpoint_id:
                    eid_groups.setdefault(dev.endpoint_id, {})
                    if platform not in eid_groups[dev.endpoint_id]:
                        eid_groups[dev.endpoint_id][platform] = dev
                    host = dev.hostname or ""
                    prev = eid_display.get(dev.endpoint_id, "")
                    if len(host) > len(prev):
                        eid_display[dev.endpoint_id] = host
                else:
                    no_eid.append((platform, dev))

        # 2b-ii. Merge no-eid devices into eid_groups by hostname match
        #        (e.g. ScreenConnect/SentinelOne have no endpoint_id but
        #        their hostname should match an ASIO/PSA eid group)
        eid_skel_map: dict[str, str] = {}  # skeleton → endpoint_id
        for eid, grp in eid_groups.items():
            for dev in grp.values():
                if dev.hostname:
                    skel = hostname_match_key(dev.hostname)
                    if skel:
                        eid_skel_map[skel] = eid

        still_no_eid: list[tuple[str, DeviceRecord]] = []
        for platform, dev in no_eid:
            merged = False
            if dev.hostname:
                skel = hostname_match_key(dev.hostname)
                if skel and skel in eid_skel_map:
                    eid = eid_skel_map[skel]
                    if platform not in eid_groups[eid]:
                        eid_groups[eid][platform] = dev
                    merged = True
                elif skel:
                    # Prefix fallback for 15-char NetBIOS truncation
                    for existing_skel, eid in eid_skel_map.items():
                        short, long = (skel, existing_skel) if len(skel) <= len(existing_skel) else (existing_skel, skel)
                        if len(short) >= _SC_NETBIOS_LIMIT and long.startswith(short):
                            if platform not in eid_groups[eid]:
                                eid_groups[eid][platform] = dev
                            merged = True
                            break
            if not merged:
                still_no_eid.append((platform, dev))

        # 2c. Build skeleton-key index from eid groups + remaining devices
        #     Each eid group becomes one skeleton entry; leftover devices
        #     go through hostname matching as before.
        skeleton_index: dict[str, dict[str, DeviceRecord]] = {}
        display_host: dict[str, str] = {}  # skeleton → best display hostname

        for eid, grp in eid_groups.items():
            # Use the best hostname from the group as the skeleton key
            best_host = eid_display.get(eid, "")
            skel = hostname_match_key(best_host) if best_host else eid
            if not skel:
                skel = eid
            skeleton_index[skel] = grp
            display_host[skel] = best_host

        for platform, dev in still_no_eid:
            host = dev.hostname
            if not host:
                continue
            skel = hostname_match_key(host)
            if not skel:
                continue
            skeleton_index.setdefault(skel, {})
            if platform not in skeleton_index[skel]:
                skeleton_index[skel][platform] = dev
            prev = display_host.get(skel, "")
            if len(host) > len(prev):
                display_host[skel] = host

        # 2d. Prefix merge — ScreenConnect truncates to 15-char NetBIOS names
        _prefix_merge(skeleton_index, display_host)

        # 2e. MAC address fallback — merge remaining unmatched by shared MAC
        _mac_merge(skeleton_index, display_host)

        # 2f. Classify each skeleton entry
        all_platforms = sorted(plat_devs.keys())
        total_company_platforms = len(all_platforms)
        matched: list[dict[str, Any]] = []
        mismatched: dict[str, list[dict[str, Any]]] = {p: [] for p in all_platforms}

        for skel in sorted(skeleton_index.keys()):
            present = skeleton_index[skel]
            platforms_found = sorted(present.keys())
            hostname = display_host.get(skel, skel)

            merged = _merge_device_data(hostname, present)
            merged["platforms"] = platforms_found

            if len(platforms_found) >= 2:
                merged["match_type"] = (
                    "full"
                    if len(platforms_found) >= total_company_platforms
                    else "partial"
                )
                matched.append(merged)
            else:
                merged["match_type"] = "unmatched"
                only_plat = platforms_found[0]
                mismatched[only_plat].append(merged)

        # Remove empty mismatch buckets
        mismatched = {p: devs for p, devs in mismatched.items() if devs}

        mismatch_total = sum(len(v) for v in mismatched.values())
        results.append({
            "company": company,
            "matched": matched,
            "mismatched": mismatched,
            "matched_count": len(matched),
            "mismatched_count": mismatch_total,
        })

    log.info(
        "reconciler.done",
        companies=len(results),
        total_matched=sum(r["matched_count"] for r in results),
        total_mismatched=sum(r["mismatched_count"] for r in results),
    )
    return results


def _dedup_psa(devices: list[DeviceRecord]) -> list[DeviceRecord]:
    """Remove PSA duplicate configs with identical (hostname, endpoint_id)."""
    seen: set[tuple[str, str]] = set()
    unique: list[DeviceRecord] = []
    for dev in devices:
        key = (dev.hostname, dev.endpoint_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(dev)
    return unique


def _merge_device_data(
    hostname: str, platform_records: dict[str, DeviceRecord]
) -> dict[str, Any]:
    """Merge the best data from each platform into a single device dict.

    Priority:
    - endpoint_id: Asio (canonical UUID) > PSA (custom field UUID) > SentinelOne
    - mac_address: PSA (most complete) > Asio > SentinelOne > ScreenConnect
    - serial_number: PSA > Asio > SentinelOne > ScreenConnect
    - site_name: PSA > Asio > SentinelOne > ScreenConnect
    - company_platform_id: Asio > PSA > SentinelOne > ScreenConnect
    """
    endpoint_id = ""
    mac_address = ""
    serial_number = ""
    site_name = ""
    platform_id = ""
    company_platform_id = ""
    company_name = ""

    # Prefer Asio endpoint_id and company/platform context, then fall back.
    for plat in ("asio", "connectwise_psa", "sentinelone", "screenconnect"):
        if plat in platform_records:
            dev = platform_records[plat]
            if not endpoint_id and dev.endpoint_id:
                endpoint_id = dev.endpoint_id
            if not mac_address and dev.mac_addresses:
                mac_address = ", ".join(dev.mac_addresses)
            if not serial_number and dev.serial_number:
                serial_number = dev.serial_number
            if not site_name and dev.site_name:
                site_name = dev.site_name
            if not platform_id and dev.platform_id:
                platform_id = dev.platform_id
            if not company_platform_id and getattr(dev, "company_platform_id", ""):
                company_platform_id = str(dev.company_platform_id)
            if not company_name and getattr(dev, "company_name", ""):
                company_name = str(dev.company_name)

    # Extract SentinelOne agent version from raw device data
    s1_agent_version = ""
    s1_dev = platform_records.get("sentinelone")
    if s1_dev and s1_dev.raw:
        s1_agent_version = s1_dev.raw.get("agentVersion", "")

    return {
        "hostname": hostname,
        "endpoint_id": endpoint_id,
        "platform_id": platform_id,
        "mac_address": mac_address,
        "serial_number": serial_number,
        "site_name": site_name,
        "company_platform_id": company_platform_id,
        "company_name": company_name,
        "s1_agent_version": s1_agent_version,
    }


# ── Prefix merge (NetBIOS truncation) ────────────────────────────

_SC_NETBIOS_LIMIT = 15


def _prefix_merge(
    skeleton_index: dict[str, dict[str, DeviceRecord]],
    display_host: dict[str, str],
) -> None:
    """Merge skeleton entries where a truncated hostname is a prefix of a full one.

    ScreenConnect's ``GuestMachineName`` uses the NetBIOS name, capped at
    15 characters.  This causes e.g. ``NUC1-080725-PAU`` (15 chars) to
    appear as a separate device from ``NUC1-080725-PAUL`` (16 chars).
    After skeleton-key conversion the short form is a prefix of the long
    form, so we can safely merge them when they belong to non-overlapping
    platforms within the same company.
    """
    merged_into: dict[str, str] = {}
    singles = [
        skel for skel, entry in skeleton_index.items()
        if len(entry) == 1
    ]
    singles.sort(key=len)

    for short_skel in singles:
        if short_skel in merged_into:
            continue
        # Only attempt prefix merge when the original hostname is at the
        # NetBIOS limit (exactly 15 characters).
        orig = display_host.get(short_skel, "")
        if len(orig) != _SC_NETBIOS_LIMIT:
            continue
        # Search longest-first so we pick the best (most complete) match
        for long_skel in sorted(skeleton_index.keys(), key=len, reverse=True):
            if long_skel == short_skel or long_skel in merged_into:
                continue
            if len(long_skel) <= len(short_skel):
                continue
            if not long_skel.startswith(short_skel):
                continue
            if set(skeleton_index[short_skel]) & set(skeleton_index[long_skel]):
                continue
            for plat, dev in skeleton_index[short_skel].items():
                skeleton_index[long_skel][plat] = dev
            merged_into[short_skel] = long_skel
            log.debug(
                "reconciler.prefix_merge",
                short=orig,
                long=display_host.get(long_skel, long_skel),
            )
            break

    for skel in merged_into:
        del skeleton_index[skel]
        display_host.pop(skel, None)


# ── MAC-address fallback merge ───────────────────────────────────

_MAC_BLOCKLIST: frozenset[str] = frozenset({"", "000000000000", "FFFFFFFFFFFF"})


def _mac_merge(
    skeleton_index: dict[str, dict[str, DeviceRecord]],
    display_host: dict[str, str],
) -> None:
    """Merge unmatched skeleton entries that share a physical MAC address.

    SentinelOne reports all network-interface MACs.  When a MAC from one
    unmatched device matches a MAC on another entry (on a different
    platform, same company), they almost certainly represent the same
    machine.  Only single-platform entries are candidates for merging;
    they can be absorbed into any other entry (single or multi-platform).
    """
    # Build MAC → skeleton reverse index for ALL entries (potential targets)
    mac_to_skels: dict[str, list[str]] = {}
    for skel, entry in skeleton_index.items():
        for dev in entry.values():
            for mac in dev.mac_addresses:
                if mac and mac not in _MAC_BLOCKLIST:
                    mac_to_skels.setdefault(mac, []).append(skel)

    merged_into: dict[str, str] = {}

    # Only single-platform entries are merge candidates
    for skel in list(skeleton_index.keys()):
        if skel in merged_into or len(skeleton_index[skel]) != 1:
            continue
        dev = next(iter(skeleton_index[skel].values()))
        for mac in dev.mac_addresses:
            if mac in _MAC_BLOCKLIST:
                continue
            for target_skel in mac_to_skels.get(mac, []):
                if target_skel == skel or target_skel in merged_into:
                    continue
                if target_skel not in skeleton_index:
                    continue
                if set(skeleton_index[skel]) & set(skeleton_index[target_skel]):
                    continue
                # Merge single into target
                for plat, d in skeleton_index[skel].items():
                    if plat not in skeleton_index[target_skel]:
                        skeleton_index[target_skel][plat] = d
                if len(display_host.get(skel, "")) > len(display_host.get(target_skel, "")):
                    display_host[target_skel] = display_host[skel]
                merged_into[skel] = target_skel
                log.debug(
                    "reconciler.mac_merge",
                    mac=mac,
                    merged=display_host.get(skel, skel),
                    into=display_host.get(target_skel, target_skel),
                )
                break
            if skel in merged_into:
                break

    for skel in merged_into:
        skeleton_index.pop(skel, None)
        display_host.pop(skel, None)
