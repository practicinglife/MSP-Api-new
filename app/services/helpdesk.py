"""Help desk automation: SentinelOne alert tickets that land in the PSA "Catchall" company.

SentinelOne emails one ticket per event (new threat, kill, quarantine…), all filed under Catchall. For each
incident (same machine + threat) this finds the tickets, reads the machine and threat from the email body,
looks the threat up in SentinelOne (status, classification, what each action did), resolves the machine to its
registry client / PSA company, and builds ONE change that:

1. moves every ticket to the client's PSA company,
2. merges them into one ticket (the others get status ">Closed"),
3. writes a summary note (internal analysis) on the merged ticket,
4. **only if SentinelOne reports the threat mitigated**: resolves the incident in SentinelOne (with a note), closes
   the ticket and logs a note on the PSA company. Otherwise the ticket stays open under the company for a tech.

Every run is a change request (an Editor submits, an Approver approves).
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from app.config import get_settings
from app.org_profile import PSA_CATCHALL_COMPANY_ID
from app.services import vendor_client

log = structlog.get_logger(__name__)

CATCHALL_COMPANY_ID = PSA_CATCHALL_COMPANY_ID  # 0 = not configured → no Catchall scan
CLOSED_STATUS_NAME = os.environ.get("MSP_TICKET_CLOSED_STATUS", ">Closed")
S1_SUMMARY_MARK = "sentinelone"
# Handled in SentinelOne = mitigated, or a person marked it benign / resolved the incident
MITIGATED = ("mitigated", "marked_as_benign")

_RE_THREAT_ID = re.compile(r"/threats/(\d{6,})")
_RE_MACHINE = [re.compile(r"machine:\s*([A-Za-z0-9][A-Za-z0-9_.\-]{1,62})", re.I),
               re.compile(r"\bMachine\s+([A-Za-z0-9][A-Za-z0-9_.\-]{1,62})\s+\\?\(", re.I),
               re.compile(r"machine\s+([A-Za-z0-9][A-Za-z0-9_.\-]{1,62})\s*$", re.I | re.M)]
_RE_THREAT_NAME = [re.compile(r"\bname:\s*(\S+)", re.I),
                   re.compile(r"(?:killed|quarantine|remediate|rollback)\w*\s+the\s+threat\s+(\S+)\s+on", re.I),
                   # follow-ups: "changed the analyst verdict for X from …", "changed the Incident Status for X from …"
                   re.compile(r"\bfor\s+(\S+)\s+from\b", re.I)]


def _clean(text: str) -> str:
    return (text or "").replace("\\(", "(").replace("\\)", ")").replace("\\[", "[").replace("\\]", "]")


def parse_alert(summary: str, notes: list[str]) -> dict[str, Any]:
    """Machine, threat name, threat id and event type from a SentinelOne alert ticket."""
    body = _clean("\n".join(notes))
    text = f"{summary}\n{body}"
    event = summary.split("SentinelOne -", 1)[1].strip() if "SentinelOne -" in summary else summary
    event = re.sub(r"\s*-\s*machine\s+\S+\s*$", "", event, flags=re.I).strip()
    machine = next((m.group(1) for rx in _RE_MACHINE for m in [rx.search(text)] if m), "")
    threat = next((m.group(1).rstrip(".,") for rx in _RE_THREAT_NAME for m in [rx.search(body)] if m), "")
    tid = _RE_THREAT_ID.search(body)
    return {"event": event, "machine": machine.rstrip(".,"), "threat_name": threat,
            "threat_id": tid.group(1) if tid else ""}


async def _notes(ticket_id: int) -> list[str]:
    r = await vendor_client.get("connectwise_psa", f"/service/tickets/{ticket_id}/notes", {"pageSize": 50})
    return [str(n.get("text") or "") for n in (r["body"] if r["ok"] and isinstance(r["body"], list) else [])]


async def find_incidents(ticket_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Open SentinelOne alert tickets in Catchall (or the given tickets), grouped by machine + threat."""
    if ticket_ids:
        tickets = []
        for tid in ticket_ids:
            r = await vendor_client.get("connectwise_psa", f"/service/tickets/{int(tid)}")
            if r["ok"] and isinstance(r["body"], dict):
                tickets.append(r["body"])
    elif not CATCHALL_COMPANY_ID:
        log.info("helpdesk.catchall_not_configured")
        tickets = []
    else:
        r = await vendor_client.get("connectwise_psa", "/service/tickets", {
            "conditions": f'closedFlag=false and company/id={CATCHALL_COMPANY_ID} and summary like "%SentinelOne%"',
            "pageSize": 100, "orderBy": "id asc"})
        tickets = r["body"] if r["ok"] and isinstance(r["body"], list) else []
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    unparsed: list[dict[str, Any]] = []
    for t in tickets:
        summary = str(t.get("summary") or "")
        if S1_SUMMARY_MARK not in summary.lower():
            continue
        info = parse_alert(summary, await _notes(int(t["id"])))
        row = {"id": int(t["id"]), "summary": summary, "event": info["event"],
               "created": (t.get("_info") or {}).get("dateEntered") or t.get("dateEntered"),
               "board_id": (t.get("board") or {}).get("id"), "company_id": (t.get("company") or {}).get("id")}
        if not info["machine"] and info["threat_id"]:
            # Follow-up emails (incident status / verdict changed…) carry only the threat link — ask SentinelOne
            raw = await s1_threat("", "", info["threat_id"])
            if raw:
                info["machine"] = str((raw.get("agentRealtimeInfo") or {}).get("agentComputerName") or "")
                info["threat_name"] = info["threat_name"] or str((raw.get("threatInfo") or {}).get("threatName") or "")
        if not info["machine"]:
            unparsed.append(row)
            continue
        key = (info["machine"].upper(), info["threat_name"].lower())
        g = groups.setdefault(key, {"machine": info["machine"], "threat_name": info["threat_name"],
                                    "threat_id": "", "tickets": []})
        g["threat_id"] = g["threat_id"] or info["threat_id"]
        g["tickets"].append(row)
    # Events without a threat name (e.g. "New active threat" carries it as name:) — fold into the machine's group
    by_machine: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for k in groups:
        by_machine[k[0]].append(k)
    for machine, keys in by_machine.items():
        blank = [k for k in keys if not k[1]]
        named = [k for k in keys if k[1]]
        if blank and len(named) == 1:
            for k in blank:
                g = groups.pop(k)
                groups[named[0]]["tickets"] += g["tickets"]
                groups[named[0]]["threat_id"] = groups[named[0]]["threat_id"] or g["threat_id"]
    out = list(groups.values())
    for g in out:
        g["tickets"].sort(key=lambda t: t["id"])
    if unparsed:
        out.append({"machine": "", "threat_name": "", "threat_id": "", "tickets": unparsed, "unparsed": True})
    return out


async def s1_threat(machine: str, threat_name: str, threat_id: str) -> dict[str, Any] | None:
    """The SentinelOne threat record (by id, else newest matching machine + name)."""
    if threat_id:
        r = await vendor_client.get("sentinelone", "/web/api/v2.1/threats", {"ids": threat_id})
    else:
        r = await vendor_client.get("sentinelone", "/web/api/v2.1/threats",
                                    {"computerName__contains": machine, "limit": 50, "sortBy": "createdAt",
                                     "sortOrder": "desc"})
    rows = ((r["body"] or {}).get("data") or []) if r["ok"] else []
    if not threat_id and threat_name:
        rows = [t for t in rows if str((t.get("threatInfo") or {}).get("threatName", "")).lower() == threat_name.lower()]
    return rows[0] if rows else None


def threat_summary(t: dict[str, Any]) -> dict[str, Any]:
    ti = t.get("threatInfo") or {}
    ag = t.get("agentRealtimeInfo") or {}
    actions = [{"action": a.get("action"), "status": a.get("status"),
                "counters": a.get("actionsCounters") or {}} for a in (t.get("mitigationStatus") or [])]
    return {"id": str(t.get("id")), "name": ti.get("threatName"), "classification": ti.get("classification"),
            "confidence": ti.get("confidenceLevel"), "mitigation": ti.get("mitigationStatus"),
            "mitigation_text": ti.get("mitigationStatusDescription"), "incident": ti.get("incidentStatus"),
            "verdict": ti.get("analystVerdict"), "path": ti.get("filePath"), "detected": ti.get("createdAt"),
            "machine": ag.get("agentComputerName"), "agent_id": str(ag.get("agentId") or ""),
            "site_id": str(ag.get("siteId") or ""), "site": ag.get("siteName"), "actions": actions,
            "mitigated": ti.get("mitigationStatus") in MITIGATED or ti.get("incidentStatus") == "resolved",
            "already_resolved": ti.get("incidentStatus") == "resolved"}


async def resolve_client(machine: str, threat: dict[str, Any] | None) -> dict[str, Any] | None:
    """Registry client + PSA company for the machine (S1 agent → site link → hostname)."""
    from app.db.repository import get_session_factory
    from app.db.schema import Client, ClientPlatformLink, PlatformDevice
    from app.services.av_coverage import host_key

    async with get_session_factory()() as db:
        cid = None
        how = ""
        if threat and threat.get("agent_id"):
            d = (await db.execute(select(PlatformDevice.client_id).where(
                PlatformDevice.platform == "sentinelone", PlatformDevice.platform_id == threat["agent_id"]))).first()
            cid, how = (d[0], "SentinelOne agent") if d and d[0] else (None, "")
        if not cid and threat and threat.get("site_id"):
            ln = (await db.execute(select(ClientPlatformLink.client_id).where(
                ClientPlatformLink.platform == "sentinelone", ClientPlatformLink.status == "confirmed",
                ClientPlatformLink.platform_company_id == threat["site_id"]))).first()
            cid, how = (ln[0], "SentinelOne site") if ln else (None, "")
        if not cid and machine:
            k = host_key(machine)
            owners = {r[0] for r in (await db.execute(select(PlatformDevice.client_id, PlatformDevice.hostname)))
                      .all() if r[0] and host_key(r[1] or "") == k}
            if len(owners) == 1:
                cid, how = next(iter(owners)), "hostname"
        if not cid:
            return None
        c = await db.get(Client, cid)
        psa = c.psa_company_id if c else None
        if c and not psa:
            ln = (await db.execute(select(ClientPlatformLink.platform_company_id).where(
                ClientPlatformLink.client_id == cid, ClientPlatformLink.platform == "connectwise_psa",
                ClientPlatformLink.status == "confirmed"))).first()
            psa = ln[0] if ln else None
    return {"client_id": c.id, "client_code": c.client_code, "client": c.standard_name,
            "psa_company_id": int(psa) if psa else None, "matched_by": how} if c else None


async def existing_incident_ticket(psa_company_id: int, machine: str, threat_name: str,
                                   exclude: set[int]) -> dict[str, Any] | None:
    """The open ticket an earlier triage already made for this incident under the company (follow-ups merge in)."""
    if not machine or not threat_name:
        return None
    pattern = f"%SentinelOne:%{threat_name} on {machine}%".replace('"', "")
    r = await vendor_client.get("connectwise_psa", "/service/tickets", {
        "conditions": f'closedFlag=false and company/id={int(psa_company_id)} and summary like "{pattern}"',
        "pageSize": 5, "orderBy": "id asc"})
    rows = [t for t in (r["body"] if r["ok"] and isinstance(r["body"], list) else []) if int(t["id"]) not in exclude]
    return rows[0] if rows else None


async def closed_status_id(board_id: int) -> int | None:
    r = await vendor_client.get("connectwise_psa", f"/service/boards/{board_id}/statuses", {"pageSize": 200})
    rows = r["body"] if r["ok"] and isinstance(r["body"], list) else []
    exact = next((s for s in rows if s.get("name") == CLOSED_STATUS_NAME), None)
    return int((exact or next((s for s in rows if s.get("closedStatus") and not s.get("inactive")), {})).get("id") or 0) or None


def ticket_note(inc: dict[str, Any], th: dict[str, Any] | None, client: dict[str, Any], closing: bool) -> str:
    lines = [f"SentinelOne incident - {inc['machine']} ({client['client']} / {client['client_code']})"]
    if th:
        lines += [f"Threat: {th['name']}  |  {th['classification']} ({th['confidence']})",
                  f"Path: {th['path']}", f"Detected: {th['detected']}",
                  f"SentinelOne status: {th['mitigation_text'] or th['mitigation']} | incident {th['incident']}"]
        for a in th["actions"]:
            c = a["counters"]
            lines.append(f"  {a['action']}: {a['status']} (success {c.get('success', 0)}, failed {c.get('failed', 0)}, "
                         f"not found {c.get('notFound', 0)}, pending reboot {c.get('pendingReboot', 0)})")
        s1_console = get_settings().s1_base_url.rstrip("/")
        if s1_console:
            lines.append(f"Threat: {s1_console}/analyze/threats/{th['id']}/overview")
    else:
        lines.append("Threat not found in SentinelOne - check the console.")
    lines.append("Alerts merged: " + "; ".join(f"#{t['id']} {t['event']}" for t in inc["tickets"]))
    lines.append((f"Closed: SentinelOne shows {th['mitigation_text'] or th['mitigation']}, incident "
                  f"{'resolved' if th and th['already_resolved'] else 'now resolved'}"
                  + (f", analyst verdict {th['verdict']}" if th and th.get('verdict') not in (None, '', 'undefined') else "")
                  + ".") if closing and th else
                 "LEFT OPEN: SentinelOne does not report the threat as mitigated - a technician needs to review it "
                 "in the console (mitigate / mark benign), then re-run the triage to close.")
    lines.append(f"(Help desk triage, {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC)")
    return "\n".join(lines)
