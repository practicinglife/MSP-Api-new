"""MSP pack routes that used to live in app/main.py: the dashboard, reconciliation and a legacy schema repair."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

log = structlog.get_logger(__name__)

router = APIRouter()

_NO_CACHE = {"Cache-Control": "no-cache"}


@router.get("/", tags=["dashboard"])
async def dashboard() -> FileResponse:
    """Serve the MSP operator dashboard."""
    return FileResponse("app/static/index.html", headers=_NO_CACHE)


@router.post("/api/admin/migrate", tags=["admin"], include_in_schema=False)
async def run_migrations() -> dict[str, Any]:
    """Legacy repair for two MSP tables that drifted on early PostgreSQL installs. New schema changes go
    through Alembic (``alembic/versions``)."""
    from sqlalchemy import text as sa_text

    from app.db.repository import _make_engine

    engine = _make_engine()
    applied: list[str] = []
    async with engine.connect() as conn:
        result = await conn.execute(sa_text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='daily_snapshots' "
            "ORDER BY ordinal_position"))
        actual_cols = [r[0] for r in result.all()]
        pd_result = await conn.execute(sa_text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='platform_devices' "
            "ORDER BY ordinal_position"))
        pd_cols = [r[0] for r in pd_result.all()]

    # Repair daily_snapshots if schema doesn't exactly match (extra or missing columns)
    expected = {"id", "snapshot_date", "company_canonical", "total_devices", "matched_count",
                "unmatched_count", "asio_count", "psa_count", "sc_count", "s1_count", "run_id", "taken_at"}
    if set(actual_cols) != expected:
        async with engine.begin() as conn:
            await conn.execute(sa_text("DROP TABLE IF EXISTS daily_snapshots CASCADE"))
            await conn.execute(sa_text("""
                CREATE TABLE daily_snapshots (
                    id VARCHAR(36) PRIMARY KEY,
                    snapshot_date VARCHAR(10) NOT NULL,
                    company_canonical VARCHAR(512) NOT NULL,
                    total_devices INTEGER NOT NULL DEFAULT 0,
                    matched_count INTEGER NOT NULL DEFAULT 0,
                    unmatched_count INTEGER NOT NULL DEFAULT 0,
                    asio_count INTEGER NOT NULL DEFAULT 0,
                    psa_count INTEGER NOT NULL DEFAULT 0,
                    sc_count INTEGER NOT NULL DEFAULT 0,
                    s1_count INTEGER NOT NULL DEFAULT 0,
                    run_id VARCHAR(36),
                    taken_at TIMESTAMPTZ DEFAULT NOW(),
                    CONSTRAINT uq_snapshot_company_date UNIQUE (snapshot_date, company_canonical)
                )
            """))
        log.info("admin.migrate.daily_snapshots_recreated", was=actual_cols)
        applied.append("daily_snapshots recreated with correct schema")

    # Repair platform_devices column drift used by reconciliation
    if "company_platform_id" not in pd_cols:
        async with engine.begin() as conn:
            await conn.execute(sa_text(
                "ALTER TABLE platform_devices ADD COLUMN company_platform_id VARCHAR(256) NOT NULL DEFAULT ''"))
        log.info("admin.migrate.platform_devices_column_added", column="company_platform_id")
        applied.append("platform_devices.company_platform_id added")

    return {"applied": applied, "status": "ok", "actual_columns": actual_cols, "platform_device_columns": pd_cols}


@router.post("/api/reconcile", tags=["reconciliation"])
async def reconcile(
    mode: str = Query("smart", pattern="^(full|fast|smart)$",
                      description="smart=delta fetch new IDs only (5-15s). fast=DB rematch only (2-5s). "
                                  "full=live fetch all (60-90s).")
) -> dict[str, Any]:
    """Trigger a reconciliation run.

    - **mode=smart** (default) — probes each platform for new/removed IDs (~3-8s),
      fetches full details only for new devices, rematches everything. Best balance.
    - **mode=fast** — skips all live API calls entirely, just re-runs matching
      on existing ``platform_devices``. Use when only match logic changed.
    - **mode=full** — fetches ALL devices from ALL platform APIs (60-90s).
      Use once daily or after a new platform is connected.
    """
    from app.api.connector_health import get_connectors
    from app.db.repository import get_session_factory
    from app.services.reconciliation import (
        run_fast_reconciliation,
        run_full_reconciliation,
        run_smart_reconciliation,
    )

    try:
        if mode == "fast":
            log.info("reconcile.fast.triggered")
            result = await run_fast_reconciliation()
            return {**result, "company_matches": result.get("companies", 0),
                    "device_matches": result.get("matched", 0), "errors": []}

        if mode == "smart":
            log.info("reconcile.smart.triggered")
            result = await run_smart_reconciliation(get_connectors())
            return {**result, "company_matches": result.get("companies", 0),
                    "device_matches": result.get("new_devices_fetched", 0), "errors": []}

        log.info("reconcile.full.triggered")
        report = await run_full_reconciliation(get_connectors())
        from app.db.schema import SyncRun

        async with get_session_factory()() as _s:
            _run = await _s.get(SyncRun, report.run_id)
        return {
            "run_id": report.run_id,
            "mode": "full",
            # Breakdown totals (same as the run history): companies + unique machines
            "company_matches": _run.company_match_count if _run else 0,
            "device_matches": _run.device_match_count if _run else 0,
            "errors": report.errors,
            "connector_results": [
                {"platform": cr.platform, "companies": len(cr.companies), "devices": len(cr.devices),
                 "error": cr.error, "latency_ms": cr.latency_ms}
                for cr in report.connector_results
            ],
        }
    except Exception as exc:
        import traceback

        error_message = f"{type(exc).__name__}: {exc}"
        log.error("reconcile.error", mode=mode, error=error_message, traceback=traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Reconciliation ({mode}) failed: {error_message}") from exc
