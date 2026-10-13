"""Company matching service using RapidFuzz fuzzy string comparison."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rapidfuzz import fuzz

from app.models.company import CompanyRecord
from app.models.device import DeviceRecord
from app.services.normalizers import normalize_company_name

# Increased thresholds for more conservative matching to prevent false positives
# like "AB Smith" matching incorrectly with "Smith" from other companies
FUZZY_AUTO_LINK_THRESHOLD = 95  # Was 90 - now requires 95% similarity
FUZZY_REVIEW_THRESHOLD = 90     # Was 85 - now requires 90% similarity


class MatchStatus(str, Enum):
    """Outcome of a company match attempt."""

    AUTO_LINKED = "auto_linked"
    NEEDS_REVIEW = "needs_review"
    UNMATCHED = "unmatched"


@dataclass
class CompanyMatchResult:
    """Result of matching two company records."""

    source: CompanyRecord
    target: CompanyRecord | None
    score: float
    status: MatchStatus


def build_cross_platform_aliases(
    companies_by_platform: dict[str, list[CompanyRecord]],
    existing_aliases: dict[str, str],
    threshold: float = FUZZY_REVIEW_THRESHOLD,
) -> dict[str, str]:
    """Build auto-aliases by fuzzy-matching company names across platforms.

    For each company in a lower-priority platform (e.g. SentinelOne),
    find the best fuzzy match in higher-priority platforms (e.g. ASIO,
    ConnectWise PSA).  If the match score meets *threshold* and the
    normalised names differ, an alias is created so that the
    lower-priority name resolves to the higher-priority canonical name.

    Manual aliases in *existing_aliases* always take precedence — this
    function only produces mappings for names that are **not** already
    covered.

    Returns a dict of ``variant_name → canonical_name``.
    """
    # Platform priority — higher-priority platform names become canonical.
    priority = ["connectwise_psa", "asio", "sentinelone", "screenconnect", "eset"]

    # Collect normalised names per platform
    platform_names: dict[str, set[str]] = {}
    for platform, companies in companies_by_platform.items():
        names: set[str] = set()
        for comp in companies:
            norm = normalize_company_name(comp.name) if comp.name else ""
            if norm:
                names.add(norm)
        if names:
            platform_names[platform] = names

    auto_aliases: dict[str, str] = {}

    for i, platform in enumerate(priority):
        if platform not in platform_names:
            continue
        # Collect names from all higher-priority platforms
        higher_names: set[str] = set()
        for hp in priority[:i]:
            if hp in platform_names:
                higher_names.update(platform_names[hp])
        if not higher_names:
            continue

        for name in platform_names[platform]:
            # Skip if already covered by a manual or previously-built alias
            if name in existing_aliases or name in auto_aliases:
                continue
            # Skip if an exact match exists in a higher-priority platform
            if name in higher_names:
                continue

            best_score = 0.0
            best_match = ""
            for candidate in higher_names:
                score = fuzz.token_sort_ratio(name, candidate)
                if score > best_score:
                    best_score = score
                    best_match = candidate

            if best_score >= threshold and best_match and best_match != name:
                auto_aliases[name] = existing_aliases.get(best_match, best_match)

    return auto_aliases


def build_device_cross_reference_aliases(
    platform_devices: list[tuple[str, list[DeviceRecord]]],
    existing_aliases: dict[str, str],
    min_overlap: int = 2,
) -> dict[str, str]:
    """Build auto-aliases by finding companies that share device hostnames across platforms.

    When the same physical device appears in ASIO under company "Acme Plumbing
    and Heating" and in SentinelOne under site "APH",
    those hostnames overlap.  This function detects such overlap and aliases
    the shorter / lower-priority platform's company name to the longer /
    higher-priority canonical name.

    Parameters
    ----------
    platform_devices:
        List of (platform_name, devices) tuples from each connector.
    existing_aliases:
        Manual alias map — entries here are never overwritten.
    min_overlap:
        Minimum number of shared hostnames required to create an alias
        (default 2 to avoid false positives from common hostnames).

    Returns a dict of ``variant_name → canonical_name``.
    """
    # Generic / common hostnames that appear across many unrelated companies.
    # Excluding these prevents false aliases when small companies share only
    # these ubiquitous names with a larger company (e.g. Namaste ↔ Wade Stables
    # both having dc01 and rdp1).
    _generic_hostnames: set[str] = {
        # Domain controllers
        "dc", "dc01", "dc02", "dc03", "dc1", "dc2", "dc3",
        "pdc", "bdc", "ad01", "ad02", "ad1", "ad2",
        # Remote desktop / terminal servers
        "rdp", "rdp1", "rdp2", "rdp01", "rdp02",
        "rds", "rds01", "rds02", "rds1", "rds2",
        "ts", "ts01", "ts02", "ts1", "ts2",
        "terminal", "termserv", "remote",
        # Generic servers
        "server", "server01", "server02", "server03",
        "server1", "server2", "server3",
        "srv", "srv01", "srv02", "srv1", "srv2",
        "host", "host01", "host02",
        # File / print / mail servers
        "fileserver", "fs", "fs01", "fs02", "fs1",
        "printserver", "print", "print01",
        "nas", "nas01", "nas1",
        "exchange", "exch", "exch01",
        "mail", "mail01", "mail1",
        # Workstations / generic PCs
        "pc", "pc01", "pc02", "pc1", "pc2",
        "desktop", "desktop01", "desktop1",
        "workstation", "ws01", "ws1",
        "laptop", "laptop01", "laptop1",
        # Networking / infrastructure
        "fw", "fw01", "firewall",
        "switch", "switch01",
        "router", "ap", "ap01",
        "vpn", "vpn01",
        # Hyper-V / VMware hosts
        "hyperv", "hyperv01", "esxi", "esxi01",
        "vmhost", "vmhost01",
        # Backup / SQL
        "backup", "backup01", "bak", "bak01",
        "sql", "sql01", "sql02", "sql1", "sql2",
        "db", "db01", "db02", "db1",
    }

    priority_rank: dict[str, int] = {
        "connectwise_psa": 0,
        "asio": 1,
        "sentinelone": 2,
        "screenconnect": 3,
        "eset": 4,
    }

    # platform → normalised_company_name → set of lowercased hostnames
    platform_company_hosts: dict[str, dict[str, set[str]]] = {}

    for platform, devices in platform_devices:
        for dev in devices:
            if not dev.hostname:
                continue
            company_raw = dev.company_name or dev.company_platform_id or ""
            if not company_raw:
                continue
            comp_norm = normalize_company_name(company_raw)
            if not comp_norm:
                continue
            comp_norm = existing_aliases.get(comp_norm, comp_norm)
            host_lower = dev.hostname.strip().lower()
            if not host_lower:
                continue
            # Skip generic hostnames that are common across many companies
            if host_lower in _generic_hostnames:
                continue
            platform_company_hosts.setdefault(platform, {}).setdefault(comp_norm, set()).add(
                host_lower
            )

    # Sort platforms by priority (lowest rank = highest priority)
    platforms = sorted(
        platform_company_hosts.keys(),
        key=lambda p: priority_rank.get(p, 999),
    )

    auto_aliases: dict[str, str] = {}

    for idx_a, plat_a in enumerate(platforms):
        for plat_b in platforms[idx_a + 1 :]:
            for comp_a, hosts_a in platform_company_hosts[plat_a].items():
                for comp_b, hosts_b in platform_company_hosts[plat_b].items():
                    if comp_a == comp_b:
                        continue  # already the same normalised name
                    overlap = hosts_a & hosts_b
                    if len(overlap) < min_overlap:
                        continue

                    # Require overlap to be a meaningful fraction of at
                    # least one company's device set (reduces false positives
                    # from generic hostnames like SERVER, PC-01, etc.)
                    smaller = min(len(hosts_a), len(hosts_b))
                    if smaller > 0 and len(overlap) / smaller < 0.3:
                        continue

                    # Determine canonical vs variant name.
                    # If one name is a prefix/truncation of the other,
                    # ALWAYS use the longer name as canonical (handles
                    # ScreenConnect 30-char API truncation and similar).
                    if comp_b.startswith(comp_a) and len(comp_b) > len(comp_a):
                        canonical, variant = comp_b, comp_a
                    elif comp_a.startswith(comp_b) and len(comp_a) > len(comp_b):
                        canonical, variant = comp_a, comp_b
                    else:
                        # Neither is a prefix — use platform priority
                        # (plat_a is always higher priority in this loop)
                        canonical, variant = comp_a, comp_b

                    # Prevent circular / conflicting aliases
                    if variant in existing_aliases or variant in auto_aliases:
                        continue
                    if canonical in auto_aliases:
                        continue  # canonical already points elsewhere

                    resolved = existing_aliases.get(canonical, canonical)
                    auto_aliases[variant] = resolved

    return auto_aliases


def match_company(source: CompanyRecord, candidates: list[CompanyRecord]) -> CompanyMatchResult:
    """Find the best matching company from *candidates* for *source*.

    Uses ``rapidfuzz.fuzz.token_sort_ratio`` on normalised company names.

    Returns a :class:`CompanyMatchResult` with status:
    - ``AUTO_LINKED`` if score >= :data:`FUZZY_AUTO_LINK_THRESHOLD`
    - ``NEEDS_REVIEW`` if score is in the review range
    - ``UNMATCHED`` if score < :data:`FUZZY_REVIEW_THRESHOLD`
    """
    if not candidates:
        return CompanyMatchResult(
            source=source, target=None, score=0.0, status=MatchStatus.UNMATCHED
        )

    source_norm = normalize_company_name(source.name)
    best_score = 0.0
    best_candidate: CompanyRecord | None = None

    for candidate in candidates:
        # Do not match a record against itself
        if candidate.platform == source.platform and candidate.platform_id == source.platform_id:
            continue
        candidate_norm = normalize_company_name(candidate.name)
        score = fuzz.token_sort_ratio(source_norm, candidate_norm)
        if score > best_score:
            best_score = score
            best_candidate = candidate

    status: MatchStatus
    if best_score >= FUZZY_AUTO_LINK_THRESHOLD:
        status = MatchStatus.AUTO_LINKED
    elif best_score >= FUZZY_REVIEW_THRESHOLD:
        status = MatchStatus.NEEDS_REVIEW
    else:
        status = MatchStatus.UNMATCHED
        best_candidate = None

    return CompanyMatchResult(source=source, target=best_candidate, score=best_score, status=status)
