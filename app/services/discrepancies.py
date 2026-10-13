"""Discrepancy report: where Asio, ScreenConnect, ConnectWise PSA and antivirus disagree, machine by machine.

No tool is treated as the source of truth. For every registry client the stored device lists are joined:

* PSA ↔ Asio by the PSA "Endpoint ID" field, then by name (15-char host key — ScreenConnect/NetBIOS truncate);
* ScreenConnect and SentinelOne ↔ Asio by name;
* leftovers are looked up in other clients to catch "same machine, different company in another tool".

PSA rows are already only **active** Managed Workstation/Server configurations (connectors/connectwise_psa.py).
Antivirus status (incl. Hyper-V/SQL/DC exclusion) comes from the AV sweep (``av_coverage.compute_gaps``).
Machines with no Asio contact for 30+ days are reported as ``stale`` only, so retired machines don't drown real gaps.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.schema import AsioEndpointAv, Client, MachineRoleOverride, PlatformDevice
from app.services.av_coverage import EXCLUDED_ROLES, STALE_DAYS, AV_VENDORS, _parse_dt, compute_gaps, effective_roles, host_key

PSA, SC, S1, ASIO = "connectwise_psa", "screenconnect", "sentinelone", "asio"

ISSUES = {
    "missing_screenconnect": "In Asio, no ScreenConnect session",
    "not_in_psa": "In Asio, no active PSA configuration",
    "psa_duplicate": "More than one active PSA configuration for one machine",
    "psa_no_endpoint_id": "PSA configuration matched by name only — Endpoint ID empty or wrong",
    "psa_without_machine": "Active PSA configuration with no machine in Asio, ScreenConnect or SentinelOne",
    "missing_asio": "In ScreenConnect/SentinelOne/PSA but no Asio agent",
    "wrong_company": "Same machine is under a different company in another tool",
    "company_not_linked": "The tool's company isn't linked to a client — Asio has this machine under the client shown",
    "no_company": "ScreenConnect session with no company (CustomProperty1 empty)",
    "av_missing": "No antivirus (or the wrong one) where it is expected",
    "av_on_excluded_server": "Antivirus on a Hyper-V host / SQL server / domain controller — remove",
    "stale": "No Asio contact for 30+ days (probably retired)",
}


def _dev(d: Any) -> dict[str, Any]:
    return {"platform": d.platform, "platform_id": d.platform_id, "hostname": d.hostname or d.hostname_norm,
            "endpoint_id": d.endpoint_id or "", "serial": d.serial_number or "", "company_raw": d.company_raw,
            "last_seen": d.last_seen_at.isoformat() if d.last_seen_at else None}


async def build_report(session: AsyncSession, client_code: str | None = None) -> dict[str, Any]:
    clients = {c.id: c for c in (await session.execute(select(Client))).scalars()}
    rows = list((await session.execute(select(PlatformDevice).where(
        PlatformDevice.platform.in_((ASIO, PSA, SC, S1))))).scalars())
    swept = {r.endpoint_id: r for r in (await session.execute(select(AsioEndpointAv))).scalars()}
    overrides = {o.endpoint_id: o.role for o in (await session.execute(select(MachineRoleOverride))).scalars()}
    gaps = await compute_gaps(session)
    av_issue = {m["endpoint_id"]: (m, g) for g in gaps["clients"] for m in g["machines"]}

    by_client: dict[str | None, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    asio_owners: dict[str, set[str | None]] = defaultdict(set)
    for d in rows:
        by_client[d.client_id][d.platform].append(d)
        if d.platform == ASIO and host_key(d.hostname):
            asio_owners[host_key(d.hostname)].add(d.client_id)
    # Host key → owning client, only when unambiguous: one client's Asio has it and the name isn't generic
    # ("DESKTOP", "SERVER"…), so a cross-company claim is never made on a coincidental name.
    global_asio = {k: (next(iter(v)), k) for k, v in asio_owners.items() if len(v) == 1 and not _generic(k)}

    stale_before = datetime.now(UTC) - timedelta(days=STALE_DAYS)
    wanted: set[str | None] | None = None
    if client_code:
        wanted = {None} if client_code.upper() == "UNLINKED" else \
            {c.id for c in clients.values() if (c.client_code or "").upper() == client_code.upper()}
    out_clients: list[dict[str, Any]] = []
    totals: dict[str, int] = defaultdict(int)

    for cid, plats in by_client.items():
        if wanted is not None and cid not in wanted:
            continue
        c = clients.get(cid) if cid else None
        issues: dict[str, list[dict[str, Any]]] = defaultdict(list)
        asio = plats.get(ASIO, [])
        psa = list(plats.get(PSA, []))
        sc = list(plats.get(SC, []))
        s1 = list(plats.get(S1, []))

        asio_by_eid = {d.endpoint_id or d.platform_id: d for d in asio}
        asio_by_key: dict[str, Any] = {}
        for d in asio:
            asio_by_key.setdefault(host_key(d.hostname), d)
        machine: dict[str, dict[str, Any]] = {}  # asio endpoint id → what matched
        for d in asio:
            machine[d.endpoint_id or d.platform_id] = {"asio": d, "psa": [], "sc": [], "s1": [], "psa_by_name": []}

        # PSA ↔ Asio
        psa_left = []
        for p in psa:
            if p.endpoint_id and p.endpoint_id in asio_by_eid:
                machine[p.endpoint_id]["psa"].append(p)
            else:
                psa_left.append(p)
        psa_left2 = []
        for p in psa_left:
            a = asio_by_key.get(host_key(p.hostname))
            if a is not None:
                m = machine[a.endpoint_id or a.platform_id]
                m["psa"].append(p)
                m["psa_by_name"].append(p)
            else:
                psa_left2.append(p)
        # ScreenConnect / SentinelOne ↔ Asio by name
        sc_left, s1_left = [], []
        for coll, left, key in ((sc, sc_left, "sc"), (s1, s1_left, "s1")):
            for d in coll:
                a = asio_by_key.get(host_key(d.hostname))
                if a is not None:
                    machine[a.endpoint_id or a.platform_id][key].append(d)
                else:
                    left.append(d)

        counts = {"asio": len(asio), "screenconnect": len(sc), "psa": len(psa), "sentinelone": len(s1),
                  "av_expected": 0, "av_installed": 0, "excluded_servers": 0, "stale": 0}

        for eid, m in machine.items():
            a = m["asio"]
            sw = swept.get(eid)
            last = _parse_dt(sw.last_contact) if sw else None
            base = {"hostname": a.hostname, "endpoint_id": eid,
                    "last_contact": sw.last_contact if sw else None, "online": sw.online if sw else None}
            if last and last < stale_before:
                counts["stale"] += 1
                issues["stale"].append({**base, "in": sorted(k for k in ("psa", "sc", "s1") if m[k])})
                continue
            roles = effective_roles(sw.roles if sw else None, overrides.get(eid))
            if roles:
                counts["excluded_servers"] += 1
            else:
                counts["av_expected"] += 1
            ai = av_issue.get(eid)
            if ai and ai[0]["status"] == "av_on_excluded_server":
                issues["av_on_excluded_server"].append({**base, "roles": [EXCLUDED_ROLES[r] for r in roles],
                                                        "found": ai[0]["found"]})
            elif ai and ai[0]["status"] in ("no_antivirus", "wrong_antivirus"):
                issues["av_missing"].append({**base, "status": ai[0]["status"], "found": ai[0]["found"],
                                             "expected": c.av_vendor if c else None})
            elif not roles and sw is not None and c and c.av_vendor in AV_VENDORS:
                counts["av_installed"] += 1
            if not m["sc"]:
                issues["missing_screenconnect"].append(base)
            if not m["psa"]:
                issues["not_in_psa"].append(base)
            elif len(m["psa"]) > 1:
                keep = next((p for p in m["psa"] if p.endpoint_id == eid), m["psa"][0])
                issues["psa_duplicate"].append({**base, "keep": _dev(keep),
                                                "extra": [_dev(p) for p in m["psa"] if p is not keep]})
            for p in m["psa_by_name"]:
                if len(m["psa"]) == 1:
                    issues["psa_no_endpoint_id"].append({**base, "psa": _dev(p),
                                                         "psa_endpoint_id": p.endpoint_id or ""})

        # Leftovers: machines a tool has that Asio (for this client) doesn't
        sc_keys = {host_key(d.hostname) for d in sc_left}
        s1_keys = {host_key(d.hostname) for d in s1_left}
        mismatch_kind = "company_not_linked" if cid is None else "wrong_company"
        for p in psa_left2:
            k = host_key(p.hostname)
            other = global_asio.get(k)
            if other and other[0] != cid:
                issues[mismatch_kind].append({"hostname": p.hostname, "tool": "PSA", "psa": _dev(p),
                                              "tool_company": p.company_raw,
                                              "asio_company": _client_name(clients, other[0])})
            elif k in sc_keys or k in s1_keys:
                issues["missing_asio"].append({"hostname": p.hostname, "seen_in": ["PSA"] + (["ScreenConnect"] if k in sc_keys else [])
                                               + (["SentinelOne"] if k in s1_keys else []), "psa": _dev(p)})
            else:
                issues["psa_without_machine"].append({"hostname": p.hostname, "psa": _dev(p)})
        psa_keys = {host_key(p.hostname) for p in psa_left2}
        for tool, left in (("ScreenConnect", sc_left), ("SentinelOne", s1_left)):
            for d in left:
                k = host_key(d.hostname)
                other = global_asio.get(k)
                if other and other[0] != cid:
                    issues[mismatch_kind].append({"hostname": d.hostname, "tool": tool, "device": _dev(d),
                                                  "tool_company": d.company_raw,
                                                  "asio_company": _client_name(clients, other[0])})
                elif tool == "ScreenConnect" and cid is None and not (d.company_raw or "").strip():
                    issues["no_company"].append({"hostname": d.hostname, "tool": tool, "device": _dev(d)})
                elif k not in psa_keys and not (tool == "SentinelOne" and k in sc_keys):
                    issues["missing_asio"].append({"hostname": d.hostname, "seen_in": [tool], "device": _dev(d)})

        issues = {k: v for k, v in issues.items() if v}
        for k, v in issues.items():
            totals[k] += len(v)
        for k in ("asio", "screenconnect", "psa", "sentinelone", "av_expected", "av_installed",
                  "excluded_servers", "stale"):
            totals[f"count_{k}"] += counts[k]
        mismatched = sorted(k for k in ("screenconnect", "psa") if counts[k] != counts["asio"]) + \
            (["antivirus"] if c and c.av_vendor in AV_VENDORS and counts["av_installed"] != counts["av_expected"] else [])
        out_clients.append({
            "client_id": cid, "client_code": c.client_code if c else None,
            "client": c.standard_name if c else "(companies not linked to a client)",
            "av_vendor": c.av_vendor if c else None, "counts": counts, "count_mismatch": mismatched,
            "issue_counts": {k: len(v) for k, v in issues.items() if k != "stale"},
            "issues": issues if client_code else None,  # machine lists only when one client is asked for
        })
    out_clients.sort(key=lambda x: (x["client_id"] is None, -sum(x["issue_counts"].values()), x["client"] or ""))
    return {"swept_at": gaps.get("swept_at"), "issue_types": ISSUES, "totals": dict(totals), "clients": out_clients}


_GENERIC = {"DESKTOP", "LAPTOP", "SERVER", "PC", "WORKSTATION", "RECEPTION", "FRONTDESK", "FRONT", "OFFICE",
            "ADMIN", "USER", "OWNER", "MAIN", "HOST", "BACKUP", "SCANNER", "KIOSK", "CONFERENCE", "TRAINING",
            "SPARE", "LOANER", "TEST", "NEW", "OLD", "HOME", "DC", "DC1", "DC01", "FS01", "SQL", "HYPERV"}


def _generic(key: str) -> bool:
    """Names too common to prove two records are the same machine across companies."""
    return len(key) < 5 or key in _GENERIC or key.rstrip("0123456789") in _GENERIC


def _client_name(clients: dict[str, Client], cid: str | None) -> str:
    c = clients.get(cid) if cid else None
    return f"{c.standard_name} ({c.client_code})" if c else "(unlinked company)"
