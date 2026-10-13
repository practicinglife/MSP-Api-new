"""Persistent audit trail. Writes never raise into the request path — a failed audit insert is logged."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# GETs worth recording: anything that reaches a vendor or exports data in bulk.
_SENSITIVE_GET = re.compile(r"^/(api/(proxy|discovery/execute|tickets|remediation|admin)|.*export)", re.I)


def should_audit(method: str, path: str) -> bool:
    if path in ("/health",) or path.startswith("/static/"):
        return False
    return method not in ("GET", "HEAD", "OPTIONS") or bool(_SENSITIVE_GET.match(path))


def args_digest(value: Any) -> str:
    """Hash of call arguments: proves what was sent without storing secrets or customer data."""
    try:
        blob = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        blob = repr(value)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


async def record(*, principal: str, auth_kind: str, source: str, method: str, path: str, status: int,
                 ip: str | None = None, action: str | None = None, detail: dict[str, Any] | None = None) -> None:
    from app.db.repository import get_session_factory
    from app.db.schema import AuditLog

    try:
        async with get_session_factory()() as db:
            db.add(AuditLog(principal=principal[:255], auth_kind=auth_kind[:16], source=source[:16],
                            method=method[:8], path=path[:512], action=(action or None) and action[:128],
                            status=status, ip=(ip or None) and ip[:64], detail=detail))
            await db.commit()
    except Exception as exc:  # noqa: BLE001
        log.error("audit.write_failed", error=str(exc), path=path, principal=principal)


async def recent(limit: int = 200, principal: str | None = None) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.db.repository import get_session_factory
    from app.db.schema import AuditLog

    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(max(1, min(limit, 2000)))
    if principal:
        q = q.where(AuditLog.principal == principal)
    async with get_session_factory()() as db:
        rows = (await db.execute(q)).scalars().all()
    return [{"id": r.id, "at": r.at, "principal": r.principal, "auth": r.auth_kind, "source": r.source,
             "method": r.method, "path": r.path, "action": r.action, "status": r.status, "ip": r.ip,
             "detail": r.detail} for r in rows]
