"""Fast DB query endpoints — pre-defined named queries against platform_devices.

All queries read from the DB (no live API calls) so responses are sub-100ms.
These are the primary data source for the MCP agent once the DB is populated.

Endpoints:
  GET /api/db/summary               — company device counts from DB
  GET /api/db/company/{name}        — all devices for one company from DB
  GET /api/db/unmatched             — devices on only one platform
  GET /api/db/platforms             — per-platform device + company counts
  GET /api/db/last-scan             — when the DB was last populated
  GET /api/db/snapshots             — list available snapshot dates
  GET /api/db/snapshot/diff         — delta between two dates
  GET /api/db/snapshot/trend        — platform totals over last N days
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import get_session_factory
from app.db.schema import DailySnapshot, PlatformDevice, ReconciledDevice, SyncRun
from app.services.normalizers import normalize_company_name

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/db", tags=["db-query"])


async def _get_session() -> AsyncSession:  # type: ignore[return]
    factory = get_session_factory()
    async with factory() as session:
        yield session


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_age(dt: Any) -> str:
    if not dt:
        return "never"
    from datetime import UTC, datetime
    now = datetime.now(UTC)
    delta = now - (dt if dt.tzinfo else dt.replace(tzinfo=UTC))
    mins = int(delta.total_seconds() / 60)
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/last-scan", response_model=dict[str, Any])
async def last_scan(session: AsyncSession = Depends(_get_session)) -> dict[str, Any]:
    """Return when the DB was last populated via POST /api/reconcile."""
    row = (await session.execute(
        select(SyncRun)
        .where(SyncRun.status == "completed")
        .order_by(SyncRun.finished_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    device_count = (await session.execute(
        select(func.count()).select_from(PlatformDevice)
    )).scalar() or 0

    if not row:
        return {"status": "never_scanned", "device_count": device_count,
                "message": "Run POST /api/reconcile to populate the DB"}
    return {
        "status": "ok",
        "last_scan": row.finished_at.isoformat() if row.finished_at else None,
        "age": _fmt_age(row.finished_at),
        "company_matches": row.company_match_count,
        "device_matches": row.device_match_count,
        "errors": row.error_count,
        "device_count": device_count,
    }


@router.get("/summary", response_model=list[dict[str, Any]])
async def db_summary(session: AsyncSession = Depends(_get_session)) -> list[dict[str, Any]]:
    """Per-company device counts per platform — pure DB read, sub-50ms.

    Returns::

        [{"company": "acme", "total": 15,
          "asio": 5, "connectwise_psa": 5, "screenconnect": 5,
          "sentinelone": 5, "eset": 0}]
    """
    rows = (await session.execute(
        select(
            PlatformDevice.company_canonical,
            PlatformDevice.platform,
            func.count().label("cnt"),
        )
        .where(PlatformDevice.company_canonical != "")
        .group_by(PlatformDevice.company_canonical, PlatformDevice.platform)
        .order_by(PlatformDevice.company_canonical)
    )).all()

    # Pivot into per-company dict
    companies: dict[str, dict[str, Any]] = {}
    for company, platform, cnt in rows:
        if company not in companies:
            companies[company] = {"company": company, "total": 0,
                                  "asio": 0, "connectwise_psa": 0,
                                  "screenconnect": 0, "sentinelone": 0, "eset": 0}
        companies[company][platform] = companies[company].get(platform, 0) + cnt
        companies[company]["total"] += cnt

    return sorted(companies.values(), key=lambda r: r["company"])


@router.get("/company/{name}", response_model=dict[str, Any])
async def db_company(
    name: str,
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """All devices for one company from reconciled_devices — same data as UI breakdown.

    Sub-20ms. Shows each device's match_type (full/partial/unmatched) and platforms.
    """
    needle = normalize_company_name(name) or name.lower().strip()
    rows = (await session.execute(
        select(ReconciledDevice)
        .where(ReconciledDevice.company_name.ilike(f"%{needle}%"))
        .order_by(ReconciledDevice.match_type, ReconciledDevice.hostname)
    )).scalars().all()

    if not rows:
        return {"company": needle, "found": False, "devices": []}

    devices = [
        {
            "hostname": r.hostname,
            "company": r.company_name,
            "match_type": r.match_type,
            "platforms": r.platforms if isinstance(r.platforms, list) else (r.platforms.get("platforms", []) if isinstance(r.platforms, dict) else []),
            "endpoint_id": r.endpoint_id,
            "serial_number": r.serial_number,
            "mac_address": r.mac_address,
        }
        for r in rows
    ]

    matched = [d for d in devices if d["match_type"] in ("full", "partial")]
    unmatched = [d for d in devices if d["match_type"] == "unmatched"]
    platforms_present = sorted({p for d in devices for p in d["platforms"]})

    return {
        "company": rows[0].company_name,
        "found": True,
        "device_count": len(devices),
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
        "platforms_present": platforms_present,
        "matched": matched,
        "unmatched": unmatched,
        "devices": devices,
    }


@router.get("/unmatched", response_model=list[dict[str, Any]])
async def db_unmatched(
    company: str | None = Query(None, description="Filter to one company"),
    platform: str | None = Query(None, description="Only return devices whose ONLY platform matches this value (e.g. connectwise_psa, asio, screenconnect, sentinelone)"),
    session: AsyncSession = Depends(_get_session),
) -> list[dict[str, Any]]:
    """Devices on only one platform — from reconciled_devices table (same data as UI breakdown).

    Reads the pre-computed match_type='unmatched' rows written during reconcile.
    Sub-20ms. Much faster and more accurate than computing from platform_devices.
    """
    q = select(ReconciledDevice).where(ReconciledDevice.match_type == "unmatched")

    if company:
        needle = normalize_company_name(company) or company.lower().strip()
        q = q.where(ReconciledDevice.company_name.ilike(f"%{needle}%"))

    rows = (await session.execute(
        q.order_by(ReconciledDevice.company_name, ReconciledDevice.hostname)
    )).scalars().all()

    results = [
        {
            "hostname": r.hostname,
            "company": r.company_name,
            "platforms": r.platforms if isinstance(r.platforms, list) else (r.platforms.get("platforms", []) if isinstance(r.platforms, dict) else []),
            "match_type": r.match_type,
            "serial_number": r.serial_number,
            "endpoint_id": r.endpoint_id,
            "last_seen": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]

    # Apply platform filter post-query (platforms stored as JSON list)
    if platform:
        plat = platform.lower().strip()
        results = [r for r in results if r["platforms"] == [plat]]

    return results


@router.get("/platforms", response_model=list[dict[str, Any]])
async def db_platforms(session: AsyncSession = Depends(_get_session)) -> list[dict[str, Any]]:
    """Per-platform device + company counts from DB."""
    rows = (await session.execute(
        select(
            PlatformDevice.platform,
            func.count().label("device_count"),
            func.count(func.distinct(PlatformDevice.company_canonical)).label("company_count"),
            func.max(PlatformDevice.last_seen_at).label("last_seen"),
        )
        .group_by(PlatformDevice.platform)
        .order_by(PlatformDevice.platform)
    )).all()

    return [
        {
            "platform": row.platform,
            "device_count": row.device_count,
            "company_count": row.company_count,
            "last_seen": row.last_seen.isoformat() if row.last_seen else None,
            "age": _fmt_age(row.last_seen),
        }
        for row in rows
    ]


def _all_platforms() -> set[str]:
    return {"asio", "connectwise_psa", "screenconnect", "sentinelone"}


# ── Snapshot / trend endpoints ────────────────────────────────────────────────

@router.get(
    "/snapshots",
    response_model=list[str],
    summary="List all dates that have a saved daily snapshot (most recent first). Use these dates as from_date/to_date for change detection.",
)
async def db_snapshots(session: AsyncSession = Depends(_get_session)) -> list[str]:
    """List all dates that have a snapshot. Most recent first."""
    rows = (await session.execute(
        select(DailySnapshot.snapshot_date)
        .distinct()
        .order_by(DailySnapshot.snapshot_date.desc())
    )).scalars().all()
    return list(rows)


@router.get(
    "/snapshot/diff",
    response_model=dict[str, Any],
    summary="Detect changes between two snapshot dates (defaults: yesterday -> today). Returns per-company before/after/delta with flags (sc_surge, new_company, lost_company, more/less_unmatched). Use for 'what changed since <date>'.",
)
async def db_snapshot_diff(
    from_date: str | None = Query(None, description="YYYY-MM-DD — defaults to yesterday"),
    to_date: str | None = Query(None, description="YYYY-MM-DD — defaults to today"),
    min_change: int = Query(1, description="Only show rows where any count changed by >= this"),
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Compare the breakdown between two snapshot dates — same view as the breakdown page.

    Shows matched/unmatched/platform counts for every company that changed.
    Use this to answer: 'What was different last Thursday vs today?'
    """
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    d_from = from_date or yesterday
    d_to   = to_date   or today

    def _load(snap_date: str) -> dict[str, Any]:
        return {}

    rows_from_q = (await session.execute(
        select(DailySnapshot).where(DailySnapshot.snapshot_date == d_from)
    )).scalars().all()

    rows_to_q = (await session.execute(
        select(DailySnapshot).where(DailySnapshot.snapshot_date == d_to)
    )).scalars().all()

    from_map = {r.company_canonical: r for r in rows_from_q}
    to_map   = {r.company_canonical: r for r in rows_to_q}
    all_cos  = sorted(set(from_map) | set(to_map))

    def _row(r: DailySnapshot | None) -> dict:
        if not r:
            return {"total": 0, "matched": 0, "unmatched": 0, "asio": 0, "psa": 0, "sc": 0, "s1": 0}
        return {
            "total": r.total_devices, "matched": r.matched_count,
            "unmatched": r.unmatched_count, "asio": r.asio_count,
            "psa": r.psa_count, "sc": r.sc_count, "s1": r.s1_count,
        }

    changes = []
    for co in all_cos:
        b = _row(from_map.get(co))
        a = _row(to_map.get(co))
        deltas = {k: a[k] - b[k] for k in a}
        if all(abs(v) < min_change for v in deltas.values()):
            continue
        changes.append({
            "company": co,
            "before": b,
            "after": a,
            "delta": deltas,
            "flags": {
                "sc_surge":       deltas["sc"] >= 5,
                "new_company":    co not in from_map,
                "lost_company":   co not in to_map,
                "more_unmatched": deltas["unmatched"] > 0,
                "less_unmatched": deltas["unmatched"] < 0,
            },
        })

    changes.sort(key=lambda r: max(abs(v) for v in r["delta"].values()), reverse=True)

    # Totals
    def _sum_field(field: str, mapping: dict) -> int:
        return sum(getattr(r, field, 0) for r in mapping.values())

    return {
        "from_date": d_from,
        "to_date": d_to,
        "snapshots_exist": {
            "from": len(from_map) > 0,
            "to": len(to_map) > 0,
        },
        "total_companies_changed": len(changes),
        "sc_surges": sum(1 for c in changes if c["flags"]["sc_surge"]),
        "new_companies": sum(1 for c in changes if c["flags"]["new_company"]),
        "more_unmatched": sum(1 for c in changes if c["flags"]["more_unmatched"]),
        "changes": changes,
    }


@router.get(
    "/snapshot/trend",
    response_model=dict[str, Any],
    summary="Trend of breakdown totals over the last N days (matched/unmatched/per-platform counts). Optionally filter to one company. Use for spotting drift over time.",
)
async def db_snapshot_trend(
    days: int = Query(7, description="How many days of history to return", le=90),
    company: str | None = Query(None, description="Filter to one company"),
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Breakdown totals over the last N days — matched/unmatched/platform counts per day.

    Without company filter: returns daily platform totals across all companies.
    With company filter: returns that company's breakdown history day by day.
    """
    since = (date.today() - timedelta(days=days)).isoformat()

    q = select(DailySnapshot).where(DailySnapshot.snapshot_date >= since)
    if company:
        needle = normalize_company_name(company) or company.lower().strip()
        q = q.where(DailySnapshot.company_canonical.ilike(f"%{needle}%"))
    q = q.order_by(DailySnapshot.snapshot_date, DailySnapshot.company_canonical)

    rows = (await session.execute(q)).scalars().all()

    if company:
        # Per-company day-by-day history
        trend = [
            {
                "date": r.snapshot_date,
                "company": r.company_canonical,
                "total": r.total_devices,
                "matched": r.matched_count,
                "unmatched": r.unmatched_count,
                "asio": r.asio_count,
                "psa": r.psa_count,
                "sc": r.sc_count,
                "s1": r.s1_count,
            }
            for r in rows
        ]
    else:
        # Aggregate totals per day across all companies
        by_date: dict[str, dict[str, int]] = {}
        for r in rows:
            d = r.snapshot_date
            if d not in by_date:
                by_date[d] = {"total": 0, "matched": 0, "unmatched": 0, "asio": 0, "psa": 0, "sc": 0, "s1": 0}
            by_date[d]["total"]     += r.total_devices
            by_date[d]["matched"]   += r.matched_count
            by_date[d]["unmatched"] += r.unmatched_count
            by_date[d]["asio"]      += r.asio_count
            by_date[d]["psa"]       += r.psa_count
            by_date[d]["sc"]        += r.sc_count
            by_date[d]["s1"]        += r.s1_count
        trend = [{"date": d, **counts} for d, counts in sorted(by_date.items())]

    return {
        "days": days,
        "company_filter": company,
        "snapshots_available": len(trend),
        "trend": trend,
    }
