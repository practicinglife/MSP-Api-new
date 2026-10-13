"""Client master API — permanent client IDs, cross-platform links, naming standard.

Reads are served from the database only (no live vendor calls). Only
``POST /api/clients/sync`` talks to the vendors, and it pulls company lists only.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.api.connector_health import get_connectors
from app.db.repository import get_session_factory
from app.db.schema import Client, ClientPlatformLink, PlatformCompany
from app.services.client_naming import (
    clean_standard_name,
    validate_client_code,
    validate_standard_name,
)
from app.services.client_registry import (
    auto_link,
    client_compliance,
    client_device_index,
    device_coverage,
    invalidate_device_cache,
    sync_platform_companies,
)
from app.services.normalizers import normalize_company_name

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/clients", tags=["clients"])


class ClientUpdate(BaseModel):
    standard_name: str | None = None
    client_code: str | None = None
    legal_name: str | None = None
    av_vendor: str | None = None
    status: str | None = None
    naming_status: str | None = None
    notes: str | None = None


class LinkCreate(BaseModel):
    platform: str
    platform_company_id: str


async def _load_all(session: Any) -> tuple[list[Client], dict[str, list[ClientPlatformLink]], dict[tuple[str, str], PlatformCompany]]:
    clients = list((await session.execute(select(Client).order_by(Client.standard_name))).scalars())
    links: dict[str, list[ClientPlatformLink]] = defaultdict(list)
    for ln in (await session.execute(select(ClientPlatformLink))).scalars():
        links[ln.client_id].append(ln)
    pcs = {
        (pc.platform, pc.platform_company_id): pc
        for pc in (await session.execute(select(PlatformCompany))).scalars()
    }
    return clients, links, pcs


def _link_dict(ln: ClientPlatformLink, pcs: dict[tuple[str, str], PlatformCompany]) -> dict[str, Any]:
    pc = pcs.get((ln.platform, ln.platform_company_id))
    return {
        "link_id": ln.id,
        "platform": ln.platform,
        "platform_company_id": ln.platform_company_id,
        "platform_name": pc.name if pc else (ln.evidence or {}).get("platform_name"),
        "active_on_platform": bool(pc and pc.active),
        "status": ln.status,
        "method": ln.link_method,
        "confidence": ln.confidence,
        "evidence": ln.evidence,
    }


def _client_dict(
    c: Client, links: list[ClientPlatformLink], pcs: dict[tuple[str, str], PlatformCompany], detail: bool = False
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": c.id,
        "client_code": c.client_code,
        "standard_name": c.standard_name,
        "naming_status": c.naming_status,
        "legal_name": c.legal_name,
        "psa_company_id": c.psa_company_id,
        "av_vendor": c.av_vendor,
        "status": c.status,
        "compliance": client_compliance(c, links, pcs),
    }
    by_platform: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ln in links:
        if ln.status == "rejected" and not detail:
            continue
        by_platform[ln.platform].append(_link_dict(ln, pcs))
    out["platforms"] = by_platform
    if detail:
        out["notes"] = c.notes
        out["created_at"] = c.created_at
        out["updated_at"] = c.updated_at
    return out


# ── Read (DB only) ──────────────────────────────────────────────────────


@router.get("")
async def list_clients(
    issue: str | None = Query(None, description="Only clients whose issues contain this text, e.g. 'missing', 'name-drift', 'review'"),
    compliant: bool | None = Query(None),
) -> dict[str, Any]:
    """All clients with their platform links and naming-standard compliance (from DB, fast)."""
    async with get_session_factory()() as session:
        clients, links, pcs = await _load_all(session)
    rows = [_client_dict(c, links.get(c.id, []), pcs) for c in clients]
    if compliant is not None:
        rows = [r for r in rows if r["compliance"]["compliant"] == compliant]
    if issue:
        rows = [r for r in rows if any(issue in i for i in r["compliance"]["issues"])]
    return {"count": len(rows), "clients": rows}


@router.get("/summary")
async def clients_summary() -> dict[str, Any]:
    """Counts of clients, compliance, and issue types."""
    async with get_session_factory()() as session:
        clients, links, pcs = await _load_all(session)
    issue_counts: dict[str, int] = defaultdict(int)
    compliant = 0
    for c in clients:
        comp = client_compliance(c, links.get(c.id, []), pcs)
        compliant += comp["compliant"]
        for i in comp["issues"]:
            issue_counts[i.split(":")[0] + (":" + i.split(":")[1] if i.startswith(("missing", "antivirus")) else "")] += 1
    linked = {(ln.platform, ln.platform_company_id) for ls in links.values() for ln in ls if ln.status != "rejected"}
    unlinked = defaultdict(int)
    for (p, pcid), pc in pcs.items():
        if pc.active and (p, pcid) not in linked:
            unlinked[p] += 1
    last_sync = max((pc.last_seen_at for pc in pcs.values()), default=None)
    return {
        "clients": len(clients),
        "compliant": compliant,
        "non_compliant": len(clients) - compliant,
        "issues": dict(sorted(issue_counts.items(), key=lambda kv: -kv[1])),
        "unlinked_platform_companies": dict(unlinked),
        "last_company_sync": last_sync,
    }


@router.get("/naming")
async def naming_report(only_drift: bool = Query(True)) -> dict[str, Any]:
    """Standard Name vs the current name in every linked platform."""
    async with get_session_factory()() as session:
        clients, links, pcs = await _load_all(session)
    rows = []
    for c in clients:
        comp = client_compliance(c, links.get(c.id, []), pcs)
        if only_drift and not comp["name_drift"] and not validate_standard_name(c.standard_name):
            continue
        current: dict[str, list[str]] = defaultdict(list)
        for ln in links.get(c.id, []):
            if ln.status == "confirmed":
                pc = pcs.get((ln.platform, ln.platform_company_id))
                if pc:
                    current[ln.platform].append(pc.name)
        rows.append({
            "id": c.id,
            "client_code": c.client_code,
            "standard_name": c.standard_name,
            "naming_status": c.naming_status,
            "standard_name_problems": validate_standard_name(c.standard_name),
            "current": current,
        })
    return {"count": len(rows), "clients": rows}


@router.get("/naming/export.csv")
async def naming_export() -> StreamingResponse:
    """CSV worklist: one row per client with each platform's current name."""
    async with get_session_factory()() as session:
        clients, links, pcs = await _load_all(session)
    platforms = ["connectwise_psa", "asio", "screenconnect", "sentinelone", "eset"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["client_id", "client_code", "standard_name", "naming_status", "name_problems", *platforms, "issues"])
    for c in clients:
        ls = links.get(c.id, [])
        cur = defaultdict(list)
        for ln in ls:
            if ln.status == "confirmed":
                pc = pcs.get((ln.platform, ln.platform_company_id))
                cur[ln.platform].append(pc.name if pc else ln.platform_company_id)
        comp = client_compliance(c, ls, pcs)
        w.writerow([
            c.id, c.client_code or "", c.standard_name, c.naming_status,
            "; ".join(validate_standard_name(c.standard_name)),
            *["; ".join(cur.get(p, [])) for p in platforms],
            " | ".join(comp["issues"]),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=client-naming.csv"},
    )


@router.get("/unlinked")
async def unlinked_companies(platform: str | None = None) -> dict[str, Any]:
    """Platform companies not linked to any client (orphans / need manual link)."""
    async with get_session_factory()() as session:
        _, links, pcs = await _load_all(session)
    linked = {(ln.platform, ln.platform_company_id) for ls in links.values() for ln in ls if ln.status != "rejected"}
    rows = [
        {"platform": pc.platform, "platform_company_id": pc.platform_company_id, "name": pc.name, "extra": pc.extra}
        for key, pc in pcs.items()
        if pc.active and key not in linked and (platform is None or pc.platform == platform)
    ]
    rows.sort(key=lambda r: (r["platform"], r["name"].lower()))
    return {"count": len(rows), "companies": rows}


@router.get("/review")
async def links_to_review() -> dict[str, Any]:
    """Suggested links waiting for confirm/reject, lowest confidence first."""
    async with get_session_factory()() as session:
        clients, links, pcs = await _load_all(session)
    names = {c.id: c.standard_name for c in clients}
    rows = [
        {"client_id": ln.client_id, "client": names.get(ln.client_id), **_link_dict(ln, pcs)}
        for ls in links.values() for ln in ls if ln.status == "suggested"
    ]
    rows.sort(key=lambda r: r["confidence"])
    return {"count": len(rows), "links": rows}


@router.get("/resolve")
async def resolve_client(
    platform: str,
    platform_company_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Find the client for a platform company — by ID (exact) or by name (via links)."""
    if not platform_company_id and not name:
        raise HTTPException(400, "Give platform_company_id or name")
    async with get_session_factory()() as session:
        clients, links, pcs = await _load_all(session)
    by_id = {c.id: c for c in clients}
    target_ids: set[str] = set()
    if platform_company_id:
        target_ids.add(platform_company_id)
    if name:
        norm = normalize_company_name(name)
        for (p, pcid), pc in pcs.items():
            if p == platform and (pc.name.strip().casefold() == name.strip().casefold() or (norm and pc.name_norm == norm)):
                target_ids.add(pcid)
    for ls in links.values():
        for ln in ls:
            if ln.platform == platform and ln.platform_company_id in target_ids and ln.status == "confirmed":
                c = by_id[ln.client_id]
                return {"match": "confirmed_link", **_client_dict(c, links[c.id], pcs)}
    raise HTTPException(404, "No confirmed client link for that platform company")


@router.get("/search")
async def search_clients(q: str = Query(..., min_length=2, description="Name, client code, abbreviation, or any platform's name for the client")) -> dict[str, Any]:
    """Find clients by standard name, code, or the name they have in any linked tool."""
    async with get_session_factory()() as session:
        clients, links, pcs = await _load_all(session)
    ql = q.strip().casefold()
    qn = normalize_company_name(q)
    scored: list[tuple[int, Client]] = []
    for c in clients:
        names = [c.standard_name, c.legal_name or ""] + [
            pcs[(ln.platform, ln.platform_company_id)].name
            for ln in links.get(c.id, [])
            if ln.status == "confirmed" and (ln.platform, ln.platform_company_id) in pcs
        ]
        score = 0
        if (c.client_code or "").casefold() == ql:
            score = 100
        for n in names:
            nl = n.casefold()
            if nl == ql or (qn and normalize_company_name(n) == qn):
                score = max(score, 90)
            elif ql in nl:
                score = max(score, 60)
            elif qn and qn in normalize_company_name(n):
                score = max(score, 50)
        if score:
            scored.append((score, c))
    scored.sort(key=lambda t: (-t[0], t[1].standard_name))
    return {
        "count": len(scored),
        "clients": [
            {"score": s, "id": c.id, "client_code": c.client_code, "standard_name": c.standard_name,
             "av_vendor": c.av_vendor, "issues": client_compliance(c, links.get(c.id, []), pcs)["issues"]}
            for s, c in scored[:25]
        ],
    }


@router.get("/devices/find")
async def find_device(hostname: str = Query(..., min_length=2)) -> dict[str, Any]:
    """Find a device by (partial) hostname across every platform, with its client."""
    async with get_session_factory()() as session:
        devices = await client_device_index(session)
        clients = {c.id: c for c in (await session.execute(select(Client))).scalars()}
    q = hostname.strip().upper()
    hits = [d for d in devices if q in (d["hostname"] or "").upper()][:200]
    for d in hits:
        c = clients.get(d["client_id"]) if d["client_id"] else None
        d["client"] = {"id": c.id, "client_code": c.client_code, "standard_name": c.standard_name} if c else None
    return {"count": len(hits), "devices": hits}


@router.get("/{client_id}/devices")
async def client_devices(
    client_id: str,
    platform: str | None = Query(None),
    missing_only: bool = Query(False, description="Only hosts missing a required tool"),
) -> dict[str, Any]:
    """All stored devices for a client across platforms + per-host coverage (DB only)."""
    async with get_session_factory()() as session:
        clients = list((await session.execute(select(Client))).scalars())
        c = next((c for c in clients if c.id == client_id or (c.client_code or "") == client_id.upper()), None)
        if not c:
            raise HTTPException(404, "Client not found")
        devices = [d for d in await client_device_index(session) if d["client_id"] == c.id]
    cov = device_coverage(devices, c.av_vendor)
    if missing_only:
        cov["hosts"] = [h for h in cov["hosts"] if h["missing"]]
    if platform:
        devices = [d for d in devices if d["platform"] == platform]
    return {
        "client": {"id": c.id, "client_code": c.client_code, "standard_name": c.standard_name, "av_vendor": c.av_vendor},
        "coverage": cov,
        "devices": devices if platform else None,
        "note": "From the last reconciliation's stored device data; run a reconcile to refresh devices.",
    }


@router.get("/{client_id}")
async def get_client(client_id: str) -> dict[str, Any]:
    async with get_session_factory()() as session:
        clients, links, pcs = await _load_all(session)
    c = next((c for c in clients if c.id == client_id or (c.client_code or "") == client_id.upper()), None)
    if not c:
        raise HTTPException(404, "Client not found")
    return _client_dict(c, links.get(c.id, []), pcs, detail=True)


# ── Write (DB only — nothing is pushed to vendor platforms) ──────────────


@router.post("/sync")
async def sync_clients(link: bool = Query(True, description="Also run auto-link after refreshing company lists")) -> dict[str, Any]:
    """Refresh cached company lists from every connector (company lists only — fast),
    then auto-link. Safe to run any time; confirmed links and approved names are kept."""
    async with get_session_factory()() as session:
        sync = await sync_platform_companies(session, get_connectors())
        linked = await auto_link(session) if link else None
    return {"sync": sync, "auto_link": linked}


@router.post("/auto-link")
async def run_auto_link() -> dict[str, Any]:
    """Re-run auto-linking from cached company lists + stored device evidence (no vendor calls)."""
    async with get_session_factory()() as session:
        return await auto_link(session)


@router.patch("/{client_id}")
async def update_client(client_id: str, body: ClientUpdate) -> dict[str, Any]:
    """Edit a client. Approving a name (naming_status=approved) locks it from auto-updates."""
    async with get_session_factory()() as session:
        c = await session.get(Client, client_id)
        if not c:
            raise HTTPException(404, "Client not found")
        data = body.model_dump(exclude_unset=True)
        if "standard_name" in data:
            data["standard_name"] = (data["standard_name"] or "").strip()
            problems = validate_standard_name(data["standard_name"])
            if problems:
                raise HTTPException(422, {"standard_name": problems, "suggestion": clean_standard_name(data["standard_name"])[:30]})
        if "client_code" in data and data["client_code"]:
            data["client_code"] = data["client_code"].strip().upper()
            problems = validate_client_code(data["client_code"])
            if problems:
                raise HTTPException(422, {"client_code": problems})
            clash = (await session.execute(
                select(Client).where(Client.client_code == data["client_code"], Client.id != client_id)
            )).scalar_one_or_none()
            if clash:
                raise HTTPException(409, f"Client code {data['client_code']} already used by {clash.standard_name}")
        if data.get("av_vendor") not in (None, "sentinelone", "eset"):
            raise HTTPException(422, "av_vendor must be sentinelone or eset")
        if data.get("naming_status") not in (None, "proposed", "approved"):
            raise HTTPException(422, "naming_status must be proposed or approved")
        for k, v in data.items():
            setattr(c, k, v)
        c.updated_at = datetime.now(UTC)
        await session.commit()
        clients, links, pcs = await _load_all(session)
    return _client_dict(c, links.get(c.id, []), pcs, detail=True)


@router.post("/{client_id}/links")
async def add_link(client_id: str, body: LinkCreate) -> dict[str, Any]:
    """Manually link a platform company to this client (confirmed)."""
    async with get_session_factory()() as session:
        c = await session.get(Client, client_id)
        if not c:
            raise HTTPException(404, "Client not found")
        pc = (await session.execute(select(PlatformCompany).where(
            PlatformCompany.platform == body.platform,
            PlatformCompany.platform_company_id == body.platform_company_id,
        ))).scalar_one_or_none()
        if not pc:
            raise HTTPException(404, "Platform company not in cache — run /api/clients/sync or check /api/clients/unlinked")
        now = datetime.now(UTC)
        # A platform company belongs to one client: reject any other client's links to it.
        others = (await session.execute(select(ClientPlatformLink).where(
            ClientPlatformLink.platform == body.platform,
            ClientPlatformLink.platform_company_id == body.platform_company_id,
        ))).scalars().all()
        mine = None
        for ln in others:
            if ln.client_id == client_id:
                mine = ln
            elif ln.status != "rejected":
                ln.status = "rejected"
                ln.updated_at = now
        if mine:
            mine.status, mine.link_method, mine.confidence, mine.updated_at = "confirmed", "manual", 1.0, now
        else:
            mine = ClientPlatformLink(
                id=str(uuid4()), client_id=client_id, platform=body.platform,
                platform_company_id=body.platform_company_id, status="confirmed",
                link_method="manual", confidence=1.0, evidence={"platform_name": pc.name},
                created_at=now, updated_at=now,
            )
            session.add(mine)
        if body.platform == "connectwise_psa" and not c.psa_company_id:
            c.psa_company_id = body.platform_company_id
        await session.commit()
        return {"status": "linked", "link_id": mine.id}


async def _set_link_status(link_id: str, status: str) -> dict[str, Any]:
    async with get_session_factory()() as session:
        ln = await session.get(ClientPlatformLink, link_id)
        if not ln:
            raise HTTPException(404, "Link not found")
        ln.status = status
        ln.updated_at = datetime.now(UTC)
        if status == "confirmed":
            ln.link_method = ln.link_method if ln.link_method.endswith("+manual") else ln.link_method + "+manual"
        await session.commit()
        return {"link_id": link_id, "status": status}


@router.post("/links/{link_id}/confirm")
async def confirm_link(link_id: str) -> dict[str, Any]:
    return await _set_link_status(link_id, "confirmed")


@router.post("/links/{link_id}/reject")
async def reject_link(link_id: str) -> dict[str, Any]:
    """Reject a link; auto-link will never suggest this pairing again."""
    return await _set_link_status(link_id, "rejected")
