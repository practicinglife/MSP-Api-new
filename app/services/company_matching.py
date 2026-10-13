"""Company matching, stored: automatic links, people's decisions, and the review list.

:mod:`app.services.company_linker` decides; this module loads its inputs from the database and keeps the
results there:

* automatic links are rows in ``company_aliases`` with ``source="auto"``, the reason in ``note`` and the
  score in ``score``; they are recalculated on every refresh, so they follow the data;
* links people make are ``source="manual"`` and always win;
* ``company_match_decisions`` remembers "these two are different" and "not a managed client", so the same
  question is never asked twice.

Everything here reads the local device copy (``platform_devices``); nothing calls a vendor.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.schema import CompanyAlias, CompanyMatchDecision, PlatformDevice
from app.models.device import DeviceRecord
from app.services.company_linker import (
    DeviceEvidence,
    LinkSuggestion,
    all_company_names,
    resolve_aliases,
    suggest_links,
    unmatched_names,
)
from app.services.normalizers import normalize_company_name

log = structlog.get_logger(__name__)

DIFFERENT = "different"
NOT_MANAGED = "not_managed"


def evidence_from_records(devices: Iterable[DeviceRecord]) -> list[DeviceEvidence]:
    return [DeviceEvidence(d.platform, d.company_name or d.company_platform_id or "", d.hostname or "",
                           d.serial_number or "", d.endpoint_id or "") for d in devices]


async def evidence_from_db(session: AsyncSession) -> list[DeviceEvidence]:
    rows = (await session.execute(select(
        PlatformDevice.platform, PlatformDevice.company_raw, PlatformDevice.company_canonical,
        PlatformDevice.hostname, PlatformDevice.serial_number, PlatformDevice.endpoint_id,
    ))).all()
    return [DeviceEvidence(p, raw or canonical or "", host or "", serial or "", endpoint or "")
            for p, raw, canonical, host, serial, endpoint in rows]


async def _state(session: AsyncSession) -> tuple[list[CompanyAlias], set[tuple[str, str]], set[str]]:
    aliases = list((await session.execute(select(CompanyAlias))).scalars())
    decisions = list((await session.execute(select(CompanyMatchDecision))).scalars())
    rejected = {(d.name, d.target) for d in decisions if d.decision == DIFFERENT}
    ignored = {d.name for d in decisions if d.decision == NOT_MANAGED}
    return aliases, rejected, ignored


async def refresh_auto_links(session: AsyncSession, devices: list[DeviceEvidence] | None = None, *,
                             apply: bool = True) -> tuple[dict[str, str], list[LinkSuggestion]]:
    """Recalculate automatic links from ``devices`` (default: the stored device copy) and save them.

    ``apply`` re-points stored devices when the links changed; reconciliation passes False because it is
    about to rewrite those rows itself.

    Returns (the alias map to use — manual + automatic, chains resolved — and the suggestions that still need
    a person). Manual links are never touched; automatic links whose evidence is gone are removed.
    """
    if devices is None:
        devices = await evidence_from_db(session)
    aliases, rejected, ignored = await _state(session)
    manual = {a.alias_name: a.canonical_name for a in aliases if a.source != "auto"}
    suggestions = suggest_links(devices, aliases=manual, rejected=rejected, ignored=ignored)
    auto = {s.name: s for s in suggestions if s.auto and s.name not in manual}

    existing_auto = {a.alias_name: a for a in aliases if a.source == "auto"}
    changed = False
    for name, row in existing_auto.items():
        if name not in auto:
            await session.delete(row)
            changed = True
    for name, s in auto.items():
        row = existing_auto.get(name)
        if row is None:
            session.add(CompanyAlias(id=str(uuid4()), alias_name=name, canonical_name=s.target, note=s.reason,
                                     source="auto", score=s.score))
            changed = True
        else:
            changed = changed or row.canonical_name != s.target
            row.canonical_name, row.note, row.score = s.target, s.reason, s.score
    await session.commit()
    if changed and apply:
        await apply_to_devices(session)
    alias_map = resolve_aliases({**{n: s.target for n, s in auto.items()}, **manual})
    review = [s for s in suggestions if not s.auto and s.name not in manual]
    log.info("company_matching.refreshed", auto=len(auto), manual=len(manual), review=len(review))
    return alias_map, review


async def alias_map(session: AsyncSession) -> dict[str, str]:
    aliases, _, _ = await _state(session)
    return resolve_aliases({a.alias_name: a.canonical_name for a in aliases})


async def apply_to_devices(session: AsyncSession) -> int:
    """Re-point stored devices at their company after links change (no vendor calls). Returns rows changed."""
    amap = await alias_map(session)
    rows = (await session.execute(select(PlatformDevice))).scalars().all()
    changed = 0
    for row in rows:
        norm = normalize_company_name(row.company_raw or "")
        if not norm:
            continue
        canonical = amap.get(norm, norm)
        if canonical != row.company_canonical:
            row.company_canonical = canonical
            changed += 1
    await session.commit()
    return changed


async def overview(session: AsyncSession) -> dict[str, Any]:
    """Everything the "Match companies" screen shows."""
    devices = await evidence_from_db(session)
    amap, review = await refresh_auto_links(session, devices)
    aliases, _rejected, ignored = await _state(session)
    decisions = list((await session.execute(select(CompanyMatchDecision))).scalars())
    reviewed = {s.name for s in review}
    unmatched = [u for u in unmatched_names(devices, aliases=amap)
                 if u["name"] not in ignored and u["name"] not in reviewed]
    companies = all_company_names(devices, aliases=amap)
    return {
        "has_data": bool(devices),
        "counts": {"devices": len(devices), "companies": len(companies),
                   "auto": sum(a.source == "auto" for a in aliases),
                   "manual": sum(a.source != "auto" for a in aliases),
                   "review": len(review), "unmatched": len(unmatched), "not_managed": len(ignored)},
        "review": [s.as_dict() for s in review],
        "unmatched": unmatched,
        "links": sorted(({"id": a.id, "name": a.alias_name, "target": a.canonical_name, "source": a.source,
                          "score": a.score, "reason": a.note or ""} for a in aliases),
                        key=lambda r: (r["source"] != "auto", r["target"], r["name"])),
        "not_managed": sorted(d.name for d in decisions if d.decision == NOT_MANAGED),
        "companies": companies,
    }


# ── People's answers ───────────────────────────────────────────────────────────────────────────────


def _norm(name: str) -> str:
    n = normalize_company_name(name)
    if not n:
        raise ValueError("A company name is required")
    return n


async def _decide(session: AsyncSession, name: str, target: str, decision: str, by: str) -> None:
    await session.execute(delete(CompanyMatchDecision).where(
        CompanyMatchDecision.name == name, CompanyMatchDecision.target == target,
        CompanyMatchDecision.decision == decision))
    session.add(CompanyMatchDecision(id=str(uuid4()), name=name, target=target, decision=decision, decided_by=by))


async def link(session: AsyncSession, name: str, target: str, by: str = "", reason: str = "") -> dict[str, str]:
    """``name`` is the same client as ``target`` (a person said so)."""
    name, target = _norm(name), _norm(target)
    if name == target:
        raise ValueError("Those are the same name already")
    amap = await alias_map(session)
    if amap.get(target, target) == name:
        raise ValueError(f'"{target}" is already linked to "{name}" — link the other way round')
    await session.execute(delete(CompanyAlias).where(CompanyAlias.alias_name == name))
    await session.execute(delete(CompanyMatchDecision).where(
        CompanyMatchDecision.name == name, CompanyMatchDecision.decision == NOT_MANAGED))
    note = f"Linked by {by}" + (f" ({reason})" if reason else "") if by else (reason or None)
    session.add(CompanyAlias(id=str(uuid4()), alias_name=name, canonical_name=target, note=note,
                             source="manual"))
    await session.commit()
    await apply_to_devices(session)
    return {"name": name, "target": target}


async def mark_different(session: AsyncSession, name: str, target: str, by: str = "") -> None:
    """``name`` is NOT ``target``: drop any automatic link between them and never suggest it again."""
    name, target = _norm(name), _norm(target)
    await session.execute(delete(CompanyAlias).where(
        CompanyAlias.alias_name == name, CompanyAlias.canonical_name == target, CompanyAlias.source == "auto"))
    await _decide(session, name, target, DIFFERENT, by)
    await session.commit()
    await apply_to_devices(session)


async def unlink(session: AsyncSession, name: str, by: str = "") -> bool:
    """Remove ``name``'s link. An automatic one is also remembered as "different" so it doesn't come back."""
    name = _norm(name)
    row = (await session.execute(select(CompanyAlias).where(CompanyAlias.alias_name == name))).scalar_one_or_none()
    if row is None:
        return False
    if row.source == "auto":
        await _decide(session, name, row.canonical_name, DIFFERENT, by)
    await session.delete(row)
    await session.commit()
    await apply_to_devices(session)
    return True


async def mark_not_managed(session: AsyncSession, name: str, by: str = "") -> None:
    await _decide(session, _norm(name), "", NOT_MANAGED, by)
    await session.commit()


async def restore(session: AsyncSession, name: str) -> None:
    """Undo "not a managed client" or "different" answers for ``name``."""
    await session.execute(delete(CompanyMatchDecision).where(CompanyMatchDecision.name == _norm(name)))
    await session.commit()


async def by_tool(session: AsyncSession) -> dict[str, Any]:
    """Matching organised by what each tool is for (the Get started "Match companies" step).

    Management tools (PSA, Asio, ScreenConnect) normally carry the same client names: their companies are the
    client list. Every company in a security tool (a SentinelOne site, an ESET company …) is matched to one of
    those clients — by the same name, an automatic link, or a person's pick — and each one that isn't settled
    comes with its best candidates for a dropdown. Management companies found in only one management tool are
    listed the same way.
    """
    from app.services.builtin_integrations import ROLES
    from app.services.company_linker import Ranker

    devices = await evidence_from_db(session)
    amap, _review = await refresh_auto_links(session, devices)
    aliases, _rejected, ignored = await _state(session)
    links = {a.alias_name: a for a in aliases}

    names_by_tool: dict[str, dict[str, int]] = {}
    for d in devices:
        norm = normalize_company_name(d.company)
        if norm:
            tool = names_by_tool.setdefault(d.platform, {})
            tool[norm] = tool.get(norm, 0) + 1
    management = [p for p in names_by_tool if ROLES.get(p) == "management"]
    security = [p for p in names_by_tool if ROLES.get(p) == "security"]
    canonical_tools: dict[str, set[str]] = {}
    for tool, names in names_by_tool.items():
        for n in names:
            canonical_tools.setdefault(amap.get(n, n), set()).add(tool)
    clients = sorted(c for c, tools in canonical_tools.items() if tools & set(management))
    ranker = Ranker(devices, amap)

    def row(tool: str, name: str, count: int, choices: list[str]) -> dict[str, Any]:
        target = amap.get(name, name)
        link = links.get(name)
        base = {"name": name, "devices": count, "tool": tool}
        if name in ignored:
            return {**base, "status": "not_managed"}
        if target in choices and target != name:
            return {**base, "status": "linked", "target": target, "source": link.source if link else "auto",
                    "reason": (link.note if link else "") or ""}
        if target in choices or (target == name and name in choices):
            return {**base, "status": "same_name", "target": target}
        cands = ranker.rank(name, choices, limit=6)
        best = cands[0] if cands and int(cands[0]["score"]) >= 60 else None  # type: ignore[call-overload]
        return {**base, "status": "needs_match", "candidates": cands, "suggested": best["name"] if best else ""}

    tools_out = []
    for tool in security:
        rows = [row(tool, n, c, clients) for n, c in sorted(names_by_tool[tool].items())]
        tools_out.append({"tool": tool, "role": "security", "rows": rows})
    if len(management) >= 2:
        for tool in management:
            others = sorted(c for c in clients if canonical_tools[c] & (set(management) - {tool}))
            rows = []
            for n, c in sorted(names_by_tool[tool].items()):
                target = amap.get(n, n)
                if len(canonical_tools.get(target, set()) & set(management)) >= 2 and n not in ignored:
                    continue  # in more than one management tool: a client, nothing to ask
                rows.append(row(tool, n, c, others))
            tools_out.append({"tool": tool, "role": "management", "rows": rows})
    pending = sum(1 for t in tools_out for r in t["rows"] if r["status"] == "needs_match")
    return {"has_data": bool(devices), "management_tools": management, "security_tools": security,
            "clients": clients, "tools": tools_out, "pending": pending}
