"""Layer 2 fingerprint inspection / refresh API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.companies import _get_session
from app.db.agent_repository import PlatformFingerprintRepository
from app.services.delta_refresh import refresh_fingerprints

router = APIRouter(prefix="/api/fingerprints", tags=["delta-refresh"])


@router.post("/refresh", summary="Recompute per-company per-platform fingerprints")
async def refresh(session: AsyncSession = Depends(_get_session)) -> dict[str, Any]:
    """Recompute fingerprints from the local ``platform_devices`` table.

    Returns the list of (platform, company) pairs whose device set changed
    since the last check. Cheap — no upstream API calls.
    """
    return await refresh_fingerprints(session)


@router.get("", summary="List all stored fingerprints")
async def list_fingerprints(
    session: AsyncSession = Depends(_get_session),
) -> list[dict[str, Any]]:
    repo = PlatformFingerprintRepository(session)
    rows = await repo.list_all()
    return [
        {
            "platform": r.platform,
            "company": r.company_canonical,
            "device_count": r.device_count,
            "devices_hash": r.devices_hash[:12],
            "last_checked_at": r.last_checked_at.isoformat(),
            "last_changed_at": r.last_changed_at.isoformat(),
        }
        for r in rows
    ]
