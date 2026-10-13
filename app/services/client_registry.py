"""Client master registry — permanent client IDs linked to each platform by ID.

Flow:
1. ``sync_platform_companies`` — pull only the *company lists* from each connector
   (seconds, not the 15-25 min device reconcile) into ``platform_companies``.
2. ``auto_link`` — Asio is the anchor (every Asio company is a managed client that must
   also exist in PSA + ScreenConnect + one of SentinelOne/ESET).  Other platforms'
   companies are linked to the Asio company — and thus the client — using device
   evidence already stored in ``platform_devices`` first (PSA Endpoint ID == Asio
   endpoint, shared hostnames), then aliases / names.  Confirmed links are never
   overwritten; rejected links are never re-suggested.
3. ``client_compliance`` — naming-standard checks per client.
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import AbstractPlatformConnector
from app.db.schema import Client, ClientPlatformLink, CompanyAlias, PlatformCompany, PlatformDevice
from app.services.client_naming import (
    clean_standard_name,
    names_equivalent,
    suggest_client_code,
    validate_client_code,
    validate_standard_name,
)
from app.services.normalizers import hostname_match_key, normalize_company_name

log = structlog.get_logger(__name__)

ANCHOR = "asio"
PSA = "connectwise_psa"
SC = "screenconnect"
REQUIRED_PLATFORMS = (PSA, ANCHOR, SC)
AV_PLATFORMS = ("sentinelone", "eset")
CONFIRM_THRESHOLD = 0.9
_SYNC_TIMEOUT_S = 180


def _now() -> datetime:
    return datetime.now(UTC)


# ── 1. Sync company lists ────────────────────────────────────────────────


async def _fetch_platform_companies(connector: AbstractPlatformConnector) -> list[dict[str, Any]]:
    """Return [{platform_company_id, name, extra}] with the platform's *exact* names."""
    platform = connector.platform_name
    if platform == SC and hasattr(connector, "_fetch_sessions"):
        sessions = await connector._fetch_sessions()  # noqa: SLF001
        groups: dict[str, dict[str, Any]] = {}
        for s in sessions:
            cp = s.get("CustomPropertyValues") or []
            cp1 = (str(cp[0]) if len(cp) > 0 and cp[0] else "").strip()
            if not cp1:
                cp1 = "(blank CustomProperty1)"
            g = groups.setdefault(cp1, {"session_count": 0, "sites": set(), "asio_site_ids": set()})
            g["session_count"] += 1
            if len(cp) > 1 and cp[1]:
                g["sites"].add(str(cp[1]).strip())
            if len(cp) > 4 and cp[4]:
                g["asio_site_ids"].add(str(cp[4]).strip())
        return [
            {
                "platform_company_id": name,
                "name": name,
                "extra": {
                    "session_count": g["session_count"],
                    "sites": sorted(g["sites"])[:50],
                    "asio_site_ids": sorted(g["asio_site_ids"])[:50],
                },
            }
            for name, g in groups.items()
        ]

    rows: list[dict[str, Any]] = []
    for rec in await connector.get_companies():
        raw = rec.raw if isinstance(rec.raw, dict) else {}
        name = str(raw.get("name") or raw.get("Name") or rec.name or "").strip()
        extra: dict[str, Any] = {}
        if platform == PSA:
            extra["identifier"] = raw.get("identifier")
            site = raw.get("site")
            if isinstance(site, dict):
                extra["default_site"] = site.get("name")
        rows.append({"platform_company_id": str(rec.platform_id), "name": name, "extra": extra})
    return rows


async def sync_platform_companies(
    session: AsyncSession, connectors: list[AbstractPlatformConnector]
) -> dict[str, Any]:
    """Refresh ``platform_companies`` from every connector's company list."""

    async def _one(c: AbstractPlatformConnector) -> tuple[str, list[dict[str, Any]] | Exception]:
        try:
            return c.platform_name, await asyncio.wait_for(_fetch_platform_companies(c), _SYNC_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            return c.platform_name, exc

    results = await asyncio.gather(*(_one(c) for c in connectors))
    now = _now()
    summary: dict[str, Any] = {}
    for platform, rows in results:
        if isinstance(rows, Exception):
            log.warning("clients.sync.platform_failed", platform=platform, error=str(rows))
            summary[platform] = {"status": "error", "error": str(rows) or type(rows).__name__}
            continue
        existing = {
            pc.platform_company_id: pc
            for pc in (
                await session.execute(select(PlatformCompany).where(PlatformCompany.platform == platform))
            ).scalars()
        }
        seen: set[str] = set()
        inserted = updated = 0
        for r in rows:
            pcid = r["platform_company_id"]
            if not pcid or pcid in seen:
                continue
            seen.add(pcid)
            pc = existing.get(pcid)
            if pc is None:
                session.add(
                    PlatformCompany(
                        id=str(uuid4()),
                        platform=platform,
                        platform_company_id=pcid,
                        name=r["name"],
                        name_norm=normalize_company_name(r["name"]),
                        extra=r.get("extra") or None,
                        active=True,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                inserted += 1
            else:
                pc.name = r["name"]
                pc.name_norm = normalize_company_name(r["name"])
                pc.extra = r.get("extra") or None
                pc.active = True
                pc.last_seen_at = now
                updated += 1
        deactivated = 0
        for pcid, pc in existing.items():
            if pcid not in seen and pc.active:
                pc.active = False
                deactivated += 1
        summary[platform] = {
            "status": "ok",
            "companies": len(seen),
            "inserted": inserted,
            "updated": updated,
            "deactivated": deactivated,
        }
    await session.commit()
    return summary


# ── 2. Evidence + auto-linking ───────────────────────────────────────────


@dataclass
class _Candidate:
    target: tuple[str, str]  # (platform, platform_company_id) of an anchor company
    method: str
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def strong_evidence(self) -> bool:
        return int(self.evidence.get("votes", 0)) >= 2


_ACRONYM_STOP = {"and", "of", "the", "for"}


def _acronym(norm: str) -> str:
    return "".join(w[0] for w in norm.split() if w not in _ACRONYM_STOP)


async def _alias_map(session: AsyncSession) -> dict[str, str]:
    rows = (await session.execute(select(CompanyAlias))).scalars().all()
    return {normalize_company_name(a.alias_name): normalize_company_name(a.canonical_name) for a in rows}


def _device_company_key(row: PlatformDevice, name_index: dict[str, dict[str, str]]) -> str | None:
    raw = row.raw_json if isinstance(row.raw_json, dict) else {}
    if row.platform == PSA:
        comp = raw.get("company")
        if isinstance(comp, dict) and comp.get("id"):
            return str(comp["id"])
    elif row.platform == "sentinelone":
        if raw.get("siteId"):
            return str(raw["siteId"])
    elif row.platform == SC:
        cp = raw.get("CustomPropertyValues") or []
        cp1 = (str(cp[0]) if cp and cp[0] else row.company_raw or "").strip()
        return cp1 or None
    if row.company_platform_id:
        return row.company_platform_id
    return name_index.get(row.platform, {}).get((row.company_raw or "").strip().casefold())


async def build_device_evidence(
    session: AsyncSession, companies: list[PlatformCompany]
) -> dict[tuple[str, str], dict[str, Any]]:
    """For each (platform, platform_company_id): votes for which Asio company it is."""
    name_index: dict[str, dict[str, str]] = defaultdict(dict)
    for pc in companies:
        name_index[pc.platform][pc.name.strip().casefold()] = pc.platform_company_id

    devices = (
        await session.execute(
            select(PlatformDevice).where(
                PlatformDevice.platform.in_([ANCHOR, PSA, SC, "sentinelone", "eset", "avast"])
            )
        )
    ).scalars().all()

    asio_by_ep: dict[str, str] = {}
    asio_by_host: dict[str, set[str]] = defaultdict(set)
    for d in devices:
        if d.platform != ANCHOR:
            continue
        key = _device_company_key(d, name_index)
        if not key:
            continue
        for ep in (d.endpoint_id, d.platform_id):
            if ep:
                asio_by_ep[ep.lower()] = key
        hk = hostname_match_key(d.hostname_norm or d.hostname)[:15]
        if hk:
            asio_by_host[hk].add(key)

    ev: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"devices": 0, "endpoint_votes": Counter(), "host_votes": Counter()}
    )
    for d in devices:
        if d.platform == ANCHOR:
            continue
        key = _device_company_key(d, name_index)
        if not key:
            continue
        e = ev[(d.platform, key)]
        e["devices"] += 1
        if d.platform == PSA and d.endpoint_id and d.endpoint_id.lower() in asio_by_ep:
            e["endpoint_votes"][asio_by_ep[d.endpoint_id.lower()]] += 1
            continue
        hk = hostname_match_key(d.hostname_norm or d.hostname)[:15]
        owners = asio_by_host.get(hk) if hk else None
        if owners and len(owners) == 1:
            e["host_votes"][next(iter(owners))] += 1
    return ev


def _evidence_candidate(e: dict[str, Any] | None) -> _Candidate | None:
    if not e:
        return None
    for votes, method in ((e["endpoint_votes"], "endpoint_id"), (e["host_votes"], "hostname_overlap")):
        if not votes:
            continue
        asio_id, count = votes.most_common(1)[0]
        share = count / sum(votes.values())
        if share < 0.6:
            continue
        if count >= 2:
            conf = 0.7 + 0.3 * share if method == "hostname_overlap" else 0.8 + 0.2 * share
        elif e["devices"] == 1:
            conf = 0.75 if method == "endpoint_id" else 0.6
        else:
            continue
        return _Candidate(
            (ANCHOR, asio_id),
            method,
            round(min(conf, 1.0), 3),
            {"votes": count, "votes_total": sum(votes.values()), "devices": e["devices"]},
        )
    return None


def _name_candidate(
    pc: PlatformCompany, targets: list[PlatformCompany], aliases: dict[str, str]
) -> _Candidate | None:
    def canon(n: str) -> str:
        return aliases.get(n, n)

    x = canon(pc.name_norm)
    if not x:
        return None
    best: _Candidate | None = None
    for a in targets:
        if a.platform == pc.platform:
            continue
        y = canon(a.name_norm)
        if not y:
            continue
        key = (a.platform, a.platform_company_id)
        if x == y:
            cand = _Candidate(key, "exact_name", 0.95)
        else:
            short, long_ = sorted((x, y), key=len)
            if len(short) >= 10 and long_.startswith(short):
                cand = _Candidate(key, "prefix_name", 0.9)
            elif " " not in short and 3 <= len(short) <= 6 and len(long_.split()) >= 3 and _acronym(long_) == short:
                cand = _Candidate(key, "acronym_name", 0.8)
            else:
                score = fuzz.token_sort_ratio(x, y)
                if score >= 88:
                    cand = _Candidate(key, "fuzzy_name", round(score / 100 * 0.88, 3))
                elif len(short) >= 5 and fuzz.token_set_ratio(x, y) >= 95:
                    cand = _Candidate(key, "partial_name", 0.7)
                else:
                    continue
        cand.evidence = {"compared": [pc.name, a.name]}
        if best is None or cand.confidence > best.confidence:
            best = cand
    return best


def _combine(ev: _Candidate | None, nm: _Candidate | None, same_client: bool) -> _Candidate | None:
    if ev and nm and same_client:
        return _Candidate(
            ev.target,
            f"{ev.method}+{nm.method}",
            round(min(1.0, max(ev.confidence, nm.confidence) + 0.05), 3),
            {**ev.evidence, **nm.evidence},
        )
    if ev and nm:  # disagree: strong device evidence wins, weak evidence loses to a better name match
        if not ev.strong_evidence and nm.confidence > ev.confidence:
            return _Candidate(nm.target, nm.method, min(nm.confidence, 0.85),
                              {**nm.evidence, "devices_suggest_other": list(ev.target)})
        return _Candidate(ev.target, ev.method, min(ev.confidence, 0.85),
                          {**ev.evidence, "name_suggests_other": list(nm.target), **nm.evidence})
    return ev or nm


def _link_status(cand: _Candidate) -> str:
    if cand.confidence < CONFIRM_THRESHOLD:
        return "suggested"
    weak_name = any(w in cand.method for w in ("fuzzy", "partial", "acronym"))
    if weak_name and not cand.strong_evidence:
        return "suggested"
    return "confirmed"


async def auto_link(session: AsyncSession) -> dict[str, Any]:
    """Create clients from Asio companies and link every other platform company.

    Idempotent. Never changes confirmed/rejected links or approved names.
    """
    companies = list(
        (await session.execute(select(PlatformCompany).where(PlatformCompany.active.is_(True)))).scalars()
    )
    if not any(pc.platform == ANCHOR for pc in companies):
        return {"status": "no_anchor", "detail": "No Asio companies cached — run /api/clients/sync first."}

    aliases = await _alias_map(session)
    evidence = await build_device_evidence(session, companies)
    links = list((await session.execute(select(ClientPlatformLink))).scalars())
    clients = {c.id: c for c in (await session.execute(select(Client))).scalars()}

    by_pair: dict[tuple[str, str], list[ClientPlatformLink]] = defaultdict(list)
    for ln in links:
        by_pair[(ln.platform, ln.platform_company_id)].append(ln)

    now = _now()
    stats = Counter()

    # (platform, platform_company_id) of an anchor company → client id
    anchor_client: dict[tuple[str, str], str] = {}
    targets: list[PlatformCompany] = []

    def _new_client(pc: PlatformCompany, method: str) -> None:
        client = Client(
            id=str(uuid4()),
            standard_name=clean_standard_name(pc.name) or pc.name,
            status="active",
            naming_status="proposed",
            created_at=now,
            updated_at=now,
        )
        session.add(client)
        clients[client.id] = client
        ln = ClientPlatformLink(
            id=str(uuid4()),
            client_id=client.id,
            platform=pc.platform,
            platform_company_id=pc.platform_company_id,
            status="confirmed",
            link_method=method,
            confidence=1.0,
            evidence={"platform_name": pc.name},
            created_at=now,
            updated_at=now,
        )
        session.add(ln)
        by_pair[(pc.platform, pc.platform_company_id)].append(ln)
        anchor_client[(pc.platform, pc.platform_company_id)] = client.id
        targets.append(pc)
        stats["clients_created"] += 1

    def _confirmed_client(pc: PlatformCompany) -> str | None:
        ln = next((ln for ln in by_pair[(pc.platform, pc.platform_company_id)] if ln.status == "confirmed"), None)
        return ln.client_id if ln else None

    def _candidate(pc: PlatformCompany) -> _Candidate | None:
        ev = _evidence_candidate(evidence.get((pc.platform, pc.platform_company_id)))
        nm = _name_candidate(pc, targets, aliases)
        same = bool(ev and nm and anchor_client.get(ev.target) == anchor_client.get(nm.target))
        return _combine(ev, nm, same)

    def _apply(pc: PlatformCompany, cand: _Candidate) -> str | None:
        """Create/refresh a link; returns its status, or None when skipped."""
        client_id = anchor_client[cand.target]
        existing = by_pair[(pc.platform, pc.platform_company_id)]
        prior = next((ln for ln in existing if ln.client_id == client_id), None)
        if prior and prior.status == "rejected":
            stats["skipped_rejected"] += 1
            return None
        status = _link_status(cand)
        evidence_blob = {**cand.evidence, "platform_name": pc.name}
        if prior:  # refresh an existing suggestion
            prior.status = status
            prior.link_method = cand.method
            prior.confidence = cand.confidence
            prior.evidence = evidence_blob
            prior.updated_at = now
            stats["suggestions_refreshed"] += 1
        else:
            ln = ClientPlatformLink(
                id=str(uuid4()),
                client_id=client_id,
                platform=pc.platform,
                platform_company_id=pc.platform_company_id,
                status=status,
                link_method=cand.method,
                confidence=cand.confidence,
                evidence=evidence_blob,
                created_at=now,
                updated_at=now,
            )
            session.add(ln)
            existing.append(ln)
            stats[f"linked_{status}"] += 1
        return status

    # Pass 1 — Asio is the anchor: one client per Asio company.
    for pc in (pc for pc in companies if pc.platform == ANCHOR):
        cid = _confirmed_client(pc)
        if cid:
            anchor_client[(pc.platform, pc.platform_company_id)] = cid
            targets.append(pc)
        else:
            _new_client(pc, "anchor")

    # Pass 2 — PSA: link to an Asio client, or (managed in PSA but not in Asio) become a client.
    for pc in (pc for pc in companies if pc.platform == PSA):
        cid = _confirmed_client(pc)
        if cid:
            stats["already_confirmed"] += 1
        else:
            cand = _candidate(pc)
            if cand and cand.target in anchor_client:
                if _apply(pc, cand) == "confirmed":
                    cid = anchor_client[cand.target]
            elif not any(ln.status != "rejected" for ln in by_pair[(pc.platform, pc.platform_company_id)]):
                _new_client(pc, "psa_anchor")
                stats["psa_only_clients"] += 1
                continue
        if cid:  # PSA names are usually the cleanest — make them matchable too
            anchor_client[(pc.platform, pc.platform_company_id)] = cid
            targets.append(pc)

    # Pass 3 — ScreenConnect, SentinelOne, ESET, … → nearest anchor (Asio or PSA) → client.
    for pc in companies:
        if pc.platform in (ANCHOR, PSA):
            continue
        if _confirmed_client(pc):
            stats["already_confirmed"] += 1
            continue
        cand = _candidate(pc)
        if cand is None or cand.target not in anchor_client:
            stats["unlinked"] += 1
            continue
        _apply(pc, cand)

    await session.flush()
    await _refresh_client_fields(session, clients, companies)
    await session.commit()
    return {"status": "ok", **stats}


async def _refresh_client_fields(
    session: AsyncSession, clients: dict[str, Client], companies: list[PlatformCompany]
) -> None:
    """Fill psa_company_id / av_vendor / proposed name+code from confirmed links."""
    pcs = {(pc.platform, pc.platform_company_id): pc for pc in companies}
    links = list(
        (await session.execute(select(ClientPlatformLink).where(ClientPlatformLink.status == "confirmed"))).scalars()
    )
    by_client: dict[str, list[ClientPlatformLink]] = defaultdict(list)
    for ln in links:
        by_client[ln.client_id].append(ln)

    taken_psa = {c.psa_company_id for c in clients.values() if c.psa_company_id}
    taken_codes = {c.client_code for c in clients.values() if c.client_code}

    for client in clients.values():
        cl = by_client.get(client.id, [])
        psa_links = [ln for ln in cl if ln.platform == PSA]
        if not client.psa_company_id and len(psa_links) == 1 and psa_links[0].platform_company_id not in taken_psa:
            client.psa_company_id = psa_links[0].platform_company_id
            taken_psa.add(client.psa_company_id)

        av = sorted({ln.platform for ln in cl if ln.platform in AV_PLATFORMS})
        client.av_vendor = av[0] if len(av) == 1 else ("conflict" if av else None)

        if client.naming_status != "approved":
            psa_pc = pcs.get((PSA, client.psa_company_id)) if client.psa_company_id else None
            if psa_pc and psa_pc.name:
                client.standard_name = clean_standard_name(psa_pc.name) or client.standard_name
            if not client.client_code:
                ident = str((psa_pc.extra or {}).get("identifier") or "").upper() if psa_pc else ""
                code = ident if not validate_client_code(ident) and ident not in taken_codes else ""
                code = code or suggest_client_code(client.standard_name, taken_codes)
                if code:
                    client.client_code = code
                    taken_codes.add(code)
        client.updated_at = _now()


# ── 3. Static device index (DB only) ─────────────────────────────────────

_DEVICE_CACHE: dict[str, Any] = {"at": None, "rows": None}
_DEVICE_CACHE_TTL_S = 60


async def client_device_index(session: AsyncSession) -> list[dict[str, Any]]:
    """Every stored device with the client it belongs to (via confirmed links).

    Built from ``platform_devices`` (written by reconciliation) — no vendor calls.
    Cached in-process for 60 s.
    """
    at = _DEVICE_CACHE["at"]
    if at and (_now() - at).total_seconds() < _DEVICE_CACHE_TTL_S and _DEVICE_CACHE["rows"] is not None:
        return _DEVICE_CACHE["rows"]

    companies = list((await session.execute(select(PlatformCompany))).scalars())
    name_index: dict[str, dict[str, str]] = defaultdict(dict)
    for pc in companies:
        name_index[pc.platform][pc.name.strip().casefold()] = pc.platform_company_id
    link_map = {
        (ln.platform, ln.platform_company_id): ln.client_id
        for ln in (
            await session.execute(select(ClientPlatformLink).where(ClientPlatformLink.status == "confirmed"))
        ).scalars()
    }
    rows: list[dict[str, Any]] = []
    for d in (await session.execute(select(PlatformDevice))).scalars():
        key = _device_company_key(d, name_index)
        rows.append({
            "client_id": link_map.get((d.platform, key)) if key else None,
            "platform": d.platform,
            "platform_id": d.platform_id,
            "platform_company_id": key,
            "company_raw": d.company_raw,
            "hostname": d.hostname_norm or d.hostname,
            "host_key": hostname_match_key(d.hostname_norm or d.hostname)[:15],
            "serial_number": d.serial_number,
            "endpoint_id": d.endpoint_id,
            "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
        })
    _DEVICE_CACHE.update(at=_now(), rows=rows)
    return rows


async def apply_registry_to_devices(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """Key every stored device to its registry client so the breakdown groups by client, not fuzzy names.

    For devices whose company is linked (confirmed) to a client: ``client_id`` is set and ``company_canonical``
    becomes the normalised name of the client's PSA company (falling back to legal/standard name) — still a
    normalised string, so the existing name-keyed lookups (PSA AV map, aliases, ESET Connect) keep working.
    Unlinked companies keep their alias-derived canonical name and ``client_id = None``.

    Returns {canonical: {client_id, client_code, standard_name, av_vendor}} for annotating the breakdown.
    """
    from sqlalchemy import update

    companies = list((await session.execute(select(PlatformCompany))).scalars())
    pcs = {(pc.platform, pc.platform_company_id): pc for pc in companies}
    name_index: dict[str, dict[str, str]] = defaultdict(dict)
    for pc in companies:
        name_index[pc.platform][pc.name.strip().casefold()] = pc.platform_company_id
    links = list((await session.execute(
        select(ClientPlatformLink).where(ClientPlatformLink.status == "confirmed"))).scalars())
    link_map = {(ln.platform, ln.platform_company_id): ln.client_id for ln in links}
    clients = {c.id: c for c in (await session.execute(select(Client))).scalars()}

    canon: dict[str, str] = {}
    info: dict[str, dict[str, Any]] = {}
    for cid, c in clients.items():
        psa = next((pcs.get((ln.platform, ln.platform_company_id)) for ln in links
                    if ln.client_id == cid and ln.platform == PSA), None)
        name = normalize_company_name((psa.name if psa else None) or c.legal_name or c.standard_name or "")
        if name:
            canon[cid] = name
            info[name] = {"client_id": cid, "client_code": c.client_code, "standard_name": c.standard_name,
                          "av_vendor": c.av_vendor}

    updates: list[dict[str, Any]] = []
    rows = (await session.execute(select(
        PlatformDevice.id, PlatformDevice.platform, PlatformDevice.raw_json, PlatformDevice.company_platform_id,
        PlatformDevice.company_raw, PlatformDevice.company_canonical, PlatformDevice.client_id))).all()
    linked = 0
    for r in rows:
        key = _device_company_key(r, name_index)
        cid = link_map.get((r.platform, key)) if key else None
        linked += bool(cid)
        new_canon = canon.get(cid, r.company_canonical) if cid else r.company_canonical
        if cid != r.client_id or new_canon != r.company_canonical:
            updates.append({"id": r.id, "client_id": cid, "company_canonical": new_canon})
    if updates:
        await session.execute(update(PlatformDevice), updates)
        await session.commit()
        invalidate_device_cache()
    log.info("registry.devices_keyed", devices=len(rows), linked=linked, changed=len(updates))
    return info


def invalidate_device_cache() -> None:
    _DEVICE_CACHE.update(at=None, rows=None)


def device_coverage(devices: list[dict[str, Any]], av_vendor: str | None) -> dict[str, Any]:
    """Per-hostname platform presence for one client and which required tools are missing."""
    required = list(REQUIRED_PLATFORMS) + ([av_vendor] if av_vendor in AV_PLATFORMS else [])
    hosts: dict[str, dict[str, Any]] = {}
    for d in devices:
        if not d["host_key"]:
            continue
        h = hosts.setdefault(d["host_key"], {"hostname": d["hostname"], "platforms": set()})
        h["platforms"].add(d["platform"])
    rows = []
    for h in hosts.values():
        missing = [p for p in required if p not in h["platforms"]]
        rows.append({"hostname": h["hostname"], "platforms": sorted(h["platforms"]), "missing": missing})
    rows.sort(key=lambda r: (-len(r["missing"]), r["hostname"]))
    counts = Counter(p for d in devices for p in [d["platform"]])
    return {
        "required_platforms": required,
        "device_counts": dict(counts),
        "hosts_total": len(rows),
        "hosts_fully_covered": sum(1 for r in rows if not r["missing"]),
        "hosts": rows,
    }


# ── 4. Compliance (naming standard) ──────────────────────────────────────────────


def client_compliance(
    client: Client, links: list[ClientPlatformLink], pcs: dict[tuple[str, str], PlatformCompany]
) -> dict[str, Any]:
    confirmed = [ln for ln in links if ln.status == "confirmed"]
    suggested = [ln for ln in links if ln.status == "suggested"]
    have = {ln.platform for ln in confirmed}
    issues: list[str] = []

    missing = [p for p in REQUIRED_PLATFORMS if p not in have]
    issues += [f"missing:{p}" for p in missing]
    av = sorted(have & set(AV_PLATFORMS))
    if not av:
        issues.append("missing:antivirus")
    elif len(av) > 1:
        issues.append("antivirus:both-sentinelone-and-eset")

    drift = []
    for ln in confirmed:
        pc = pcs.get((ln.platform, ln.platform_company_id))
        current = pc.name if pc else (ln.evidence or {}).get("platform_name", "")
        if current and not names_equivalent(current, client.standard_name):
            drift.append({"platform": ln.platform, "current": current})
    if drift:
        issues.append(f"name-drift:{len(drift)}")

    per_platform = Counter(ln.platform for ln in confirmed)
    for p, n in per_platform.items():
        if n > 1:
            issues.append(f"multiple-{p}-companies:{n}")

    name_problems = validate_standard_name(client.standard_name)
    code_problems = validate_client_code(client.client_code or "")
    issues += [f"standard-name:{p}" for p in name_problems]
    issues += [f"client-code:{p}" for p in code_problems]
    if suggested:
        issues.append(f"links-to-review:{len(suggested)}")

    return {
        "compliant": not issues,
        "issues": issues,
        "missing_platforms": missing,
        "antivirus": av,
        "name_drift": drift,
    }
