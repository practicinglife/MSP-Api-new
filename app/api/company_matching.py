"""Match companies: review automatic links and answer the suggestions the linker isn't sure about.

``GET /api/companies/matching`` returns everything the screen needs; the POSTs record a person's answer and
re-point the stored devices at once (no vendor calls). See :mod:`app.services.company_matching`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.auth.principal import current_principal
from app.db.repository import get_session_factory
from app.services import company_matching

router = APIRouter(prefix="/api/companies/matching", tags=["company matching"])


class NamePair(BaseModel):
    name: str
    target: str
    reason: str = ""


class Name(BaseModel):
    name: str


def _who() -> str:
    p = current_principal.get()
    return (p.name or p.id) if p else ""


async def _refresh_breakdown() -> None:
    """Recount companies and devices from the stored copy so the dashboard reflects the change."""
    from app.services.reconciliation import run_fast_reconciliation

    await run_fast_reconciliation(record_run=False)


@router.get("")
async def matching_overview() -> dict[str, Any]:
    """Automatic links (with reasons), suggestions to review, names still in only one tool, and every company."""
    async with get_session_factory()() as session:
        return await company_matching.overview(session)


@router.post("/link")
async def link_companies(body: NamePair) -> dict[str, Any]:
    """``name`` is the same client as ``target``."""
    async with get_session_factory()() as session:
        try:
            result = await company_matching.link(session, body.name, body.target, by=_who(), reason=body.reason)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    await _refresh_breakdown()
    return {"linked": result}


@router.post("/different")
async def different_companies(body: NamePair) -> dict[str, Any]:
    """``name`` is not ``target``; never suggest it again."""
    async with get_session_factory()() as session:
        try:
            await company_matching.mark_different(session, body.name, body.target, by=_who())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    await _refresh_breakdown()
    return {"ok": True}


@router.post("/unlink")
async def unlink_company(body: Name) -> dict[str, Any]:
    """Undo a link. Undoing an automatic link also stops it being made again."""
    async with get_session_factory()() as session:
        try:
            removed = await company_matching.unlink(session, body.name, by=_who())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    if not removed:
        raise HTTPException(404, "That name has no link")
    await _refresh_breakdown()
    return {"ok": True}


@router.post("/not-managed")
async def not_managed(body: Name) -> dict[str, Any]:
    """``name`` is a contact-only or one-off company: stop listing it as unmatched."""
    async with get_session_factory()() as session:
        try:
            await company_matching.mark_not_managed(session, body.name, by=_who())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    return {"ok": True}


@router.post("/restore")
async def restore_name(body: Name) -> dict[str, Any]:
    """Forget earlier "not managed" / "different" answers for ``name`` so it is suggested again."""
    async with get_session_factory()() as session:
        try:
            await company_matching.restore(session, body.name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    return {"ok": True}


@router.get("/by-tool")
async def matching_by_tool() -> dict[str, Any]:
    """Each security tool's companies (and management companies found in only one management tool), with
    how each is matched to a client — or its best candidates when it isn't yet."""
    async with get_session_factory()() as session:
        return await company_matching.by_tool(session)


class Batch(BaseModel):
    links: list[NamePair] = []
    not_managed: list[str] = []


@router.post("/apply")
async def apply_batch(body: Batch) -> dict[str, Any]:
    """Save several answers at once (the Get started wizard's "Save matches")."""
    done, errors = 0, []
    async with get_session_factory()() as session:
        for pair in body.links:
            try:
                await company_matching.link(session, pair.name, pair.target, by=_who(), reason=pair.reason)
                done += 1
            except ValueError as exc:
                errors.append(f"{pair.name}: {exc}")
        for name in body.not_managed:
            try:
                await company_matching.mark_not_managed(session, name, by=_who())
                done += 1
            except ValueError as exc:
                errors.append(f"{name}: {exc}")
    if done:
        await _refresh_breakdown()
    return {"saved": done, "errors": errors}
