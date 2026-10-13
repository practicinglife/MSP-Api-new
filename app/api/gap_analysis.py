"""Gap analysis API — cross-platform device coverage endpoints."""

from typing import Any

import structlog
from fastapi import APIRouter

from app.api.connector_health import get_connectors
from app.services.gap_analysis import build_coverage_matrix

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/coverage", tags=["coverage"])
# Legacy alias — keep /api/gap-analysis working for existing MCP tools and links
gap_analysis_router = APIRouter(prefix="/api/gap-analysis", tags=["coverage"])


@router.get("", response_model=list[dict[str, Any]])
async def coverage_matrix() -> list[dict[str, Any]]:
    """Return the full coverage matrix — device counts per company per platform."""
    connectors = get_connectors()
    rows = await build_coverage_matrix(connectors)
    return [row.model_dump() for row in rows]


@router.get("/missing", response_model=list[dict[str, Any]])
async def missing_devices() -> list[dict[str, Any]]:
    """Return only companies that are missing devices on at least one platform."""
    connectors = get_connectors()
    rows = await build_coverage_matrix(connectors)
    return [row.model_dump() for row in rows if row.missing_platforms]


@router.get(
    "/summary",
    response_model=dict[str, Any],
    summary="High-level cross-platform coverage summary (companies, platform totals)",
)
async def coverage_summary() -> dict[str, Any]:
    """Return a high-level summary of cross-platform coverage."""
    connectors = get_connectors()
    rows = await build_coverage_matrix(connectors)
    active_platforms = list({c.platform_name for c in connectors if not c.check_config()})
    total_companies = len(rows)
    fully_covered = sum(1 for r in rows if not r.missing_platforms)
    partially_covered = total_companies - fully_covered

    # Per-platform device totals
    platform_totals: dict[str, int] = {}
    for row in rows:
        for plat, count in row.platforms.items():
            platform_totals[plat] = platform_totals.get(plat, 0) + count

    return {
        "active_platforms": active_platforms,
        "total_companies": total_companies,
        "fully_covered": fully_covered,
        "partially_covered": partially_covered,
        "platform_totals": platform_totals,
    }


@router.get("/eset-licenses", response_model=list[dict[str, Any]])
async def eset_license_seats() -> list[dict[str, Any]]:
    """Return per-company ESET license seat usage (used vs total vs available).

    Calls EMA2 /api/License/List. Falls back to seat data embedded in
    the Company/Children payload if the License endpoint is unavailable.

    Response shape per company::

        {
            "company": "acme corp",
            "company_display": "Acme Corp",
            "eset_company_id": "...",
            "product": "ESET Endpoint Security",
            "seats_total": 25,
            "seats_used": 18,
            "seats_available": 7,
            "expiry_date": "2026-12-31"
        }
    """
    from app.connectors.eset import EsetConnector
    connector = EsetConnector()
    errors = connector.check_config()
    if errors:
        return [{"error": "ESET not configured", "details": errors}]
    seats = await connector.get_license_seats()
    seats.sort(key=lambda r: r.get("company", ""))
    return seats


# ── Legacy /api/gap-analysis passthrough ─────────────────────────────────────

@gap_analysis_router.get("", response_model=list[dict[str, Any]])
async def gap_analysis_legacy() -> list[dict[str, Any]]:
    """Legacy alias for /api/coverage — returns full coverage matrix."""
    connectors = get_connectors()
    rows = await build_coverage_matrix(connectors)
    return [row.model_dump() for row in rows]
