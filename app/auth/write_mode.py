"""Per-platform write mode — what may change data in a connected API.

* ``read_only`` (default for new installs): nothing writes to the platform, not even approved changes.
* ``direct``: Editors (and keys with a ``<platform>:write`` scope) may write directly — proxy, tickets,
  remediation buttons — and approved changes run.
* ``approval``: direct writes are refused; the only way to write is a change request that an Editor submits and
  an Approver approves.

Resolution: ``platform_policies`` row (set by an admin) → ``MSP_WRITE_MODE_<PLATFORM>`` → ``MSP_WRITE_MODE_DEFAULT``
→ ``read_only``.
"""

from __future__ import annotations

import os

from fastapi import HTTPException

READ_ONLY, DIRECT, APPROVAL = "read_only", "direct", "approval"
MODES = (READ_ONLY, DIRECT, APPROVAL)


def _env_mode(platform: str) -> str:
    for name in (f"MSP_WRITE_MODE_{platform.upper()}", "MSP_WRITE_MODE_DEFAULT"):
        value = os.environ.get(name, "").strip().lower()
        if value in MODES:
            return value
    return READ_ONLY


async def get_write_mode(platform: str) -> str:
    from app.db.repository import get_session_factory
    from app.db.schema import PlatformPolicy

    try:
        async with get_session_factory()() as db:
            row = await db.get(PlatformPolicy, platform)
    except Exception:  # noqa: BLE001 — no DB → fall back to configuration, never to "allow"
        row = None
    if row is not None and row.write_mode in MODES:
        return row.write_mode
    return _env_mode(platform)


async def all_write_modes(platforms: list[str]) -> dict[str, dict[str, str]]:
    from sqlalchemy import select

    from app.db.repository import get_session_factory
    from app.db.schema import PlatformPolicy

    async with get_session_factory()() as db:
        rows = {r.platform: r for r in (await db.execute(select(PlatformPolicy))).scalars()}
    out: dict[str, dict[str, str]] = {}
    for p in sorted(set(platforms) | set(rows)):
        row = rows.get(p)
        out[p] = {"write_mode": row.write_mode if row else _env_mode(p),
                  "source": "policy" if row else "environment"}
    return out


async def set_write_mode(platform: str, mode: str, actor: str) -> None:
    if mode not in MODES:
        raise HTTPException(400, f"write_mode must be one of: {', '.join(MODES)}")
    from app.db.repository import get_session_factory
    from app.db.schema import PlatformPolicy

    async with get_session_factory()() as db:
        row = await db.get(PlatformPolicy, platform)
        if row is None:
            db.add(PlatformPolicy(platform=platform, write_mode=mode, updated_by=actor))
        else:
            row.write_mode, row.updated_by = mode, actor
        await db.commit()


async def enforce_direct_write(platform: str) -> None:
    """Call before any *direct* write to a platform (not via the change engine).

    Checks the caller's role + platform scope, then the platform's write mode.
    """
    from app.auth.principal import WRITE, get_current

    principal = get_current()
    if principal is None or not principal.can_edit or not principal.allows(platform, WRITE):
        raise HTTPException(403, f"Your credential may not write to {platform}")
    mode = await get_write_mode(platform)
    if mode == DIRECT:
        return
    if mode == APPROVAL:
        raise HTTPException(409, f"{platform} requires approval for changes — propose a change request instead "
                                 "(POST /api/changes or the propose_change MCP tool)")
    raise HTTPException(403, f"{platform} is read-only. An Approver can change its write mode.")


async def enforce_change_execution(platform: str) -> None:
    """Approved change requests may write unless the platform is read-only."""
    if await get_write_mode(platform) == READ_ONLY:
        raise HTTPException(403, f"{platform} is read-only — this change cannot run until an admin "
                                 "sets its write mode to 'approval' or 'direct'")
