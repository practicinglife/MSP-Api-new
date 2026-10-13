"""Sync Runs API router — run history, drill-down, and connector events."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import SyncRunRepository, get_session_factory

router = APIRouter(prefix="/api/runs", tags=["runs"])


async def _get_session() -> AsyncSession:  # type: ignore[return]
    """Dependency that yields a database session."""
    factory = get_session_factory()
    async with factory() as session:
        yield session  # type: ignore[misc]


@router.get("", response_model=list[dict[str, Any]])
async def list_runs(session: AsyncSession = Depends(_get_session)) -> list[dict[str, Any]]:
    """Return recent sync runs with summary counts."""
    repo = SyncRunRepository(session)
    runs = await repo.list_runs()
    return [
        {
            "id": r.id,
            "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "company_match_count": r.company_match_count,
            "device_match_count": r.device_match_count,
            "error_count": r.error_count,
            "errors": r.errors,
            "warning_count": r.warning_count,
            "warnings": r.warnings,
        }
        for r in runs
    ]


@router.get("/{run_id}", response_model=dict[str, Any])
async def get_run(run_id: str, session: AsyncSession = Depends(_get_session)) -> dict[str, Any]:
    """Return a single run with its connector events."""
    repo = SyncRunRepository(session)
    run = await repo.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    events = await repo.get_run_events(run_id)
    return {
        "id": run.id,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "company_match_count": run.company_match_count,
        "device_match_count": run.device_match_count,
        "error_count": run.error_count,
        "errors": run.errors,
        "warning_count": run.warning_count,
        "warnings": run.warnings,
        "connector_events": [
            {
                "id": e.id,
                "connector": e.connector,
                "event_type": e.event_type,
                "message": e.message,
                "company_count": e.company_count,
                "device_count": e.device_count,
                "latency_ms": e.latency_ms,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ],
    }
