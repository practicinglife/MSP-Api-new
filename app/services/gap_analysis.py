"""Gap analysis service — cross-platform device coverage per company."""

from collections import defaultdict

import structlog

from app.connectors.base import AbstractPlatformConnector
from app.models.actions import CoverageRow
from app.models.company import CompanyRecord
from app.models.device import DeviceRecord
from app.services.normalizers import normalize_company_name

log = structlog.get_logger(__name__)


async def build_coverage_matrix(
    connectors: list[AbstractPlatformConnector],
) -> list[CoverageRow]:
    """Fetch companies and devices from every connector and build a coverage matrix.

    Returns a list of :class:`CoverageRow` objects — one per unique company —
    showing how many devices each platform reports for that company.
    """
    all_platforms: list[str] = [c.platform_name for c in connectors]

    # company_name → { platform → device count }
    coverage: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    # Build a company ID → name lookup per platform
    company_id_to_name: dict[str, dict[str, str]] = defaultdict(dict)

    for connector in connectors:
        missing = connector.check_config()
        if missing:
            log.info("gap_analysis.skip_unconfigured", platform=connector.platform_name)
            continue

        try:
            companies: list[CompanyRecord] = await connector.get_companies()
            devices: list[DeviceRecord] = await connector.get_devices()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "gap_analysis.fetch_error",
                platform=connector.platform_name,
                error=str(exc),
            )
            continue

        # Map company IDs to names for this platform
        for comp in companies:
            norm = normalize_company_name(comp.name) or comp.name
            company_id_to_name[connector.platform_name][comp.platform_id] = norm
            # Ensure every company appears even if it has zero devices
            coverage[norm][connector.platform_name] += 0

        # Count devices per company
        for device in devices:
            comp_name = ""
            if device.company_name:
                comp_name = normalize_company_name(device.company_name)
            elif device.company_platform_id:
                comp_name = company_id_to_name.get(
                    connector.platform_name, {}
                ).get(device.company_platform_id, "")
            if not comp_name:
                comp_name = "(unassigned)"
            coverage[comp_name][connector.platform_name] += 1

    # Build the result rows
    rows: list[CoverageRow] = []
    for company_name, platform_counts in sorted(coverage.items()):
        total = sum(platform_counts.values())
        missing = [p for p in all_platforms if p not in platform_counts or platform_counts[p] == 0]
        rows.append(
            CoverageRow(
                company_name=company_name,
                platforms=dict(platform_counts),
                total_devices=total,
                missing_platforms=missing,
            )
        )

    log.info("gap_analysis.complete", companies=len(rows), platforms=len(all_platforms))
    return rows
