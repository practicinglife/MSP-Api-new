"""Export and import connection settings (never secrets), to set up another install or share with a colleague.

``GET /api/admin/connections/export`` → a JSON file; ``POST /api/admin/connections/import`` creates each connection
in it (``?dry_run=true`` lists what would happen). Imported connections arrive without credentials: the result says
which ones need them. Admin paths: Approver or Admin (middleware).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.auth.principal import get_current
from app.connections import service
from app.connections.base import CallError, SourceError
from app.db.repository import get_session_factory
from app.db.schema import Connection, Operation

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/admin/connections", tags=["connections"])

FORMAT = "api-tools-connections/1"
# Kinds whose definition can only be read with credentials: imported as a note, added again with a template/form.
NEEDS_CREDENTIALS_TO_IMPORT = {"sql", "mongodb", "graphql", "mcp"}


def _actor() -> str:
    p = get_current()
    return p.id if p else "unknown"


@router.get("/export")
async def export_connections() -> dict[str, Any]:
    """Every connection's settings — kind, definition, address, sign-in type (not the secrets), write mode,
    pinned tools and groups. Built-in MSP tools are listed by name only."""
    from app.auth.write_mode import get_write_mode

    async with get_session_factory()() as db:
        conns = (await db.execute(select(Connection).order_by(Connection.name))).scalars().all()
        pinned: dict[str, list[dict[str, str]]] = {}
        for op in (await db.execute(select(Operation).where(Operation.pinned.is_(True)))).scalars():
            pinned.setdefault(op.connection_id, []).append({"method": op.method or "", "path": op.target or "",
                                                            "name": op.name})
    out = []
    for c in conns:
        if c.kind == "builtin":
            out.append({"slug": c.slug, "name": c.name, "kind": "builtin"})
            continue
        source = {k: v for k, v in (c.source or {}).items() if k in ("url", "text", "use_auth")}
        out.append({
            "slug": c.slug, "name": c.name, "kind": c.kind, "base_url": c.base_url, "description": c.description,
            "source": source, "auth_type": c.auth_type, "settings": c.settings or {}, "enabled": c.enabled,
            "write_mode": await get_write_mode(c.slug), "pinned": pinned.get(c.id, []),
            "needs_credentials": bool(c.secrets),
        })
    log.info("connections.exported", count=len(out), by=_actor())
    return {"format": FORMAT, "exported_at": datetime.now(UTC).isoformat(), "connections": out}


class ImportBody(BaseModel):
    format: str = FORMAT
    connections: list[dict[str, Any]] = []


@router.post("/import")
async def import_connections(body: ImportBody, dry_run: bool = False) -> dict[str, Any]:
    """Create each connection in an export file. Existing names are skipped; credentials are never in the file,
    so connections that need them are listed under ``needs_credentials``."""
    from app.auth.write_mode import MODES, set_write_mode

    if body.format != FORMAT:
        raise HTTPException(400, f"Not an API Tools connections file (expected format {FORMAT})")
    async with get_session_factory()() as db:
        existing = set((await db.execute(select(Connection.slug))).scalars())
    results = []
    for item in body.connections:
        slug, kind, name = str(item.get("slug") or ""), str(item.get("kind") or ""), str(item.get("name") or "")
        row: dict[str, Any] = {"slug": slug, "name": name, "kind": kind}
        if kind == "builtin":
            results.append({**row, "action": "skipped", "reason": "Built-in tool: connect it on this install"})
            continue
        if slug in existing:
            results.append({**row, "action": "skipped", "reason": "A connection with this name already exists"})
            continue
        if kind in NEEDS_CREDENTIALS_TO_IMPORT:
            results.append({**row, "action": "skipped",
                            "reason": "Needs its credentials to read it: add it again with a template or the form"})
            continue
        settings = dict(item.get("settings") or {})
        auth_type = str(item.get("auth_type") or "none")
        needs = auth_type != "none" or bool(item.get("needs_credentials"))
        if dry_run:
            results.append({**row, "action": "create", "needs_credentials": needs})
            continue
        try:
            await service.create_connection({
                "kind": kind, "name": name, "slug": slug, "base_url": item.get("base_url") or "",
                "source": item.get("source") or {}, "auth": {"type": "none"}, "settings": settings,
            }, _actor())
            # The sign-in type comes back without its secrets; the person adds those next.
            if auth_type != "none":
                await service.set_auth_type(slug, auth_type)
            mode = str(item.get("write_mode") or "")
            if mode in MODES:
                await set_write_mode(slug, mode, _actor())
            pinned = await service.pin_by_path(
                slug, [(p.get("method", ""), p.get("path", "")) for p in item.get("pinned") or []], _actor())
            if item.get("enabled") is False:
                await service.update_connection(slug, {"enabled": False}, _actor())
            results.append({**row, "action": "created", "needs_credentials": needs, "auth_type": auth_type,
                            "pinned": len(pinned)})
            existing.add(slug)
        except (SourceError, CallError, ValueError) as exc:
            results.append({**row, "action": "failed", "reason": str(exc)})
    log.info("connections.imported", dry_run=dry_run, created=sum(r["action"] == "created" for r in results),
             by=_actor())
    return {"dry_run": dry_run, "results": results,
            "needs_credentials": [r["slug"] for r in results if r.get("needs_credentials")]}
