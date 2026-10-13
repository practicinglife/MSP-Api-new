"""Admin endpoints: audit log and per-platform write modes. Admin/Approver only (enforced by the middleware)."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.auth import audit
from app.auth.principal import get_current
from app.auth.write_mode import MODES, all_write_modes, set_write_mode

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/audit")
async def audit_log(limit: int = 200, principal: str | None = None) -> list[dict[str, Any]]:
    """Most recent audit entries, newest first."""
    return await audit.recent(limit, principal)


@router.get("/write-modes")
async def write_modes() -> dict[str, Any]:
    from sqlalchemy import select

    from app import packs
    from app.db.repository import get_session_factory
    from app.db.schema import Connection

    platforms: list[str] = []
    if packs.is_active("msp"):  # the MSP pack's vendor connectors have write modes too
        from app.api.connector_health import get_connectors

        platforms = [c.platform_name for c in get_connectors()]
    async with get_session_factory()() as db:
        platforms += list((await db.execute(select(Connection.slug))).scalars())
    return {"modes": list(MODES), "platforms": await all_write_modes(platforms)}


class WriteModeBody(BaseModel):
    write_mode: str


@router.put("/write-modes/{platform}")
async def put_write_mode(platform: str, body: WriteModeBody) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9_\-]{1,64}", platform):
        raise HTTPException(400, "Invalid platform name")
    p = get_current()
    await set_write_mode(platform, body.write_mode.strip().lower(), p.id if p else "unknown")
    return {"platform": platform, "write_mode": body.write_mode.strip().lower()}
