"""Catchall alert triage for alerts that name the customer/site (Avast Business, HPE Instant On).

Same principle as the SentinelOne triage (services/helpdesk.py): read who the alert is about from the ticket, match
it to a ConnectWise company, and propose ONE change per alert group that moves the tickets there, merges repeats
(into an already-triaged open ticket when there is one), adds a summary note — and closes + logs on the company only
when the alert itself shows it was handled.

* **Avast Business CloudCare** — "Customer:" + "Device:" fields. Handled = the Virus Action shows Avast dealt with it
  (no action required / blocked / moved to chest / deleted / repaired / quarantined / aborted connection).
* **HPE Instant On** — "Some devices are offline on <site> site". Can't tell when the device is back, so never
  auto-closed: moved + repeats merged + note.

Company match, in order: a name a person already confirmed (``alert_company_aliases``) → exact PSA company name →
client registry (clear winner only, then shorter forms of the name: "YMCA-Knowles" → "YMCA") → unmatched (stays in
Catchall, listed with candidates so a person can pick; the pick is remembered).
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from sqlalchemy import select

from app.services import vendor_client
from app.services.helpdesk import CATCHALL_COMPANY_ID, _clean, _notes
from app.services.normalizers import normalize_company_name

log = structlog.get_logger(__name__)

AVAST_HANDLED = ("no action required", "blocked", "moved to chest", "deleted", "repaired", "quarantined",
                 "aborted connection", "connection aborted", "cured", "removed")
_RE_INSTANTON = re.compile(r"Some devices are offline on (.+?)\s+site\s*$", re.I)
_RE_FIELD = re.compile(r"\*\*(Customer|Group|Device|Date and Time|Description):\*\*\s*(.*)", re.I)
_RE_TAB = re.compile(r"^(Threat Description|Threat Severity|Threat Shield|Virus Action|Object Name):?\s*\t?(.*)$",
                     re.I | re.M)


def _handled(action: str) -> bool:
    """Avast dealt with it — but 'Threat not removed' / 'Failed to delete' must not count."""
    a = action.lower()
    if re.search(r"\b(not|no longer|fail\w*|unable|error|cannot|can't|pending)\b", a.replace("no action required", "")):
        return False
    return any(h in a for h in AVAST_HANDLED)


def parse(summary: str, notes: list[str]) -> dict[str, Any] | None:
    """{source, name, device, threat, action, handled, key, title, detail} or None if not an alert we handle."""
    body = _clean("\n".join(notes)).replace("\\_", "_")
    s = summary.replace("[External]", "").strip()
    m = _RE_INSTANTON.search(s)
    if m:
        site = m.group(1).strip()
        return {"source": "instanton", "name": re.sub(r"\s+Site$", "", site, flags=re.I).strip(), "raw_name": site,
                "device": "", "threat": "", "action": "", "handled": False, "key": f"instanton|{site.lower()}",
                "title": f"Instant On: devices offline at {site}"[:100], "event": "Devices offline"}
    if "avast" in body.lower() or "infected with a security threat" in s.lower():
        f = {k.lower(): v.strip() for k, v in _RE_FIELD.findall(body)}
        t = {k.lower(): v.strip() for k, v in _RE_TAB.findall(body)}
        customer = f.get("customer") or (s.split(" - ", 1)[0].strip() if " - " in s else "")
        device = f.get("device") or (s.rsplit(":", 1)[1].strip() if ":" in s else "")
        threat = t.get("threat description", "")
        action = t.get("virus action", "")
        if not customer:
            return None
        return {"source": "avast", "name": customer, "raw_name": customer, "device": device, "threat": threat,
                "action": action, "severity": t.get("threat severity", ""), "shield": t.get("threat shield", ""),
                "object": t.get("object name", "")[:300], "when": f.get("date and time", ""),
                "handled": _handled(action),
                "key": f"avast|{customer.lower()}|{device.lower()}|{threat.lower()}",
                "title": f"Avast: {threat or 'alert'} on {device}"[:100],
                "event": f.get("description") or "Avast alert"}
    return None


def _shorter_forms(name: str) -> list[str]:
    """'ARV S. Providence' → ['ARV S. Providence', 'ARV S', 'ARV']; 'YMCA-Knowles' → [..., 'YMCA']."""
    parts = [p for p in re.split(r"[\s\-_.,/]+", name) if p]
    return [name] + [" ".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)]


async def match_company(name: str, source: str) -> dict[str, Any]:
    """→ {psa_company_id, company, how, candidates} (psa_company_id None = needs a person)."""
    from app.api.clients import search_clients as registry_search
    from app.db.repository import get_session_factory
    from app.db.schema import AlertCompanyAlias, Client, ClientPlatformLink

    norm = normalize_company_name(name) or name.strip().lower()
    async with get_session_factory()() as db:
        alias = await db.get(AlertCompanyAlias, f"{source}|{norm}")
        if alias:
            return {"psa_company_id": alias.psa_company_id, "company": alias.company_name,
                    "how": f"confirmed by {alias.set_by}", "candidates": []}
    # Exact PSA company name
    q = name.replace('"', "")
    r = await vendor_client.get("connectwise_psa", "/company/companies",
                                {"conditions": f'name="{q}" and deletedFlag=false', "pageSize": 5,
                                 "fields": "id,name,status"})
    rows = r["body"] if r["ok"] and isinstance(r["body"], list) else []
    if len(rows) == 1:
        return {"psa_company_id": int(rows[0]["id"]), "company": rows[0]["name"], "how": "exact PSA company name",
                "candidates": []}
    # Client registry, full name then shorter forms — accept only a clear winner
    candidates: list[dict[str, Any]] = []
    async with get_session_factory()() as db:
        for form in _shorter_forms(name):
            if len(form) < 3:
                continue
            res = (await registry_search(q=form))["clients"][:5]
            hits = [h for h in res if h.get("score", 0) >= 90]
            candidates += [h for h in res if h not in candidates]
            if hits and (len(hits) == 1 or hits[0]["score"] - hits[1]["score"] >= 5):
                c = await db.get(Client, hits[0]["id"])
                psa = c.psa_company_id if c else None
                if c and not psa:
                    ln = (await db.execute(select(ClientPlatformLink.platform_company_id).where(
                        ClientPlatformLink.client_id == c.id, ClientPlatformLink.platform == "connectwise_psa",
                        ClientPlatformLink.status == "confirmed"))).first()
                    psa = ln[0] if ln else None
                if psa:
                    return {"psa_company_id": int(psa), "company": c.standard_name,
                            "how": f"client registry ({c.client_code}, matched '{form}')", "candidates": []}
    # PSA name search on the first word for suggestions
    first = _shorter_forms(name)[-1].replace('"', "")
    r = await vendor_client.get("connectwise_psa", "/company/companies",
                                {"conditions": f'name like "%{first}%" and deletedFlag=false', "pageSize": 8,
                                 "fields": "id,name,status"})
    psa_like = r["body"] if r["ok"] and isinstance(r["body"], list) else []
    sugg = [{"psa_company_id": int(x["id"]), "company": x["name"]} for x in psa_like]
    for h in candidates[:5]:
        sugg.append({"client_code": h.get("client_code"), "company": h.get("standard_name"), "score": h.get("score")})
    return {"psa_company_id": None, "company": None, "how": "no confident match", "candidates": sugg[:10]}


async def find_groups(ticket_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Open Avast / Instant On alert tickets in Catchall (or the given ones), grouped per alert."""
    if ticket_ids:
        tickets = []
        for tid in ticket_ids:
            r = await vendor_client.get("connectwise_psa", f"/service/tickets/{int(tid)}")
            if r["ok"] and isinstance(r["body"], dict):
                tickets.append(r["body"])
    elif not CATCHALL_COMPANY_ID:
        tickets = []
    else:
        r = await vendor_client.get("connectwise_psa", "/service/tickets", {
            "conditions": f"closedFlag=false and company/id={CATCHALL_COMPANY_ID} and "
                          '(summary like "%infected with a security threat%" or summary like "%devices are offline on%")',
            "pageSize": 100, "orderBy": "id asc"})
        tickets = r["body"] if r["ok"] and isinstance(r["body"], list) else []
    groups: dict[str, dict[str, Any]] = {}
    for t in tickets:
        info = parse(str(t.get("summary") or ""), await _notes(int(t["id"])))
        if not info:
            continue
        g = groups.setdefault(info["key"], {**info, "tickets": []})
        g["handled"] = g["handled"] or info["handled"]
        g["tickets"].append({"id": int(t["id"]), "summary": t.get("summary"), "event": info["event"],
                             "action": info.get("action", ""), "board_id": (t.get("board") or {}).get("id"),
                             "company_id": (t.get("company") or {}).get("id")})
    return list(groups.values())


def note_text(g: dict[str, Any], match: dict[str, Any], closing: bool) -> str:
    if g["source"] == "avast":
        lines = [f"Avast Business alert - {g['name']} / device {g['device']}",
                 f"Threat: {g['threat']} | severity {g.get('severity')} | shield {g.get('shield')}",
                 f"Object: {g.get('object')}", f"Avast action: {g['action']}", f"When: {g.get('when')}"]
    else:
        lines = [f"HPE Instant On - devices offline at site '{g['raw_name']}'",
                 "Instant On doesn't report recovery by email; check the site in the Instant On portal."]
    lines.append(f"Matched to {match['company']} ({match['how']}).")
    lines.append("Alerts merged: " + "; ".join(f"#{t['id']}" for t in g["tickets"]))
    lines.append("Closed: Avast reports it handled ('" + g["action"] + "')." if closing else
                 "Left open for a technician.")
    return "\n".join(lines)
