"""Help desk: SentinelOne alert tickets → one ticket under the right company (change request).

``s1_ticket_triage`` handles ONE incident (the alert tickets for one machine + threat). See services/helpdesk.py.
Close rule: close only when SentinelOne reports the threat **mitigated**; then the incident is resolved in
SentinelOne too and a note is logged on the PSA company. Otherwise: moved + merged + noted, left open for a tech.
"""

from __future__ import annotations

from typing import Any

from app.services.commands import Command, CommandError, Plan, register


def _http(platform: str, method: str, path: str, description: str, body: Any = None,
          before_ref: dict[str, Any] | None = None, compare_keys: list[str] | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {"kind": "http", "platform": platform, "method": method, "path": path, "params": {},
                            "body": body, "description": description, "before": None}
    if before_ref:
        step["before_ref"] = before_ref
        step["compare_keys"] = compare_keys or []
    return step


async def plan_s1_triage(p: dict[str, Any]) -> Plan:
    from app.services import helpdesk as hd
    from app.services import vendor_client

    ids = [int(i) for i in p["ticket_ids"]]
    incidents = [i for i in await hd.find_incidents(ids) if not i.get("unparsed")]
    if not incidents:
        raise CommandError("Couldn't read a machine name from these tickets — are they SentinelOne alerts?")
    if len(incidents) > 1:
        raise CommandError("These tickets are about more than one machine/threat: " +
                           "; ".join(f"{i['machine']} {i['threat_name']} (#{', #'.join(str(t['id']) for t in i['tickets'])})"
                                     for i in incidents) + " — propose each incident separately")
    inc = incidents[0]
    raw = await hd.s1_threat(inc["machine"], inc["threat_name"], inc["threat_id"])
    th = hd.threat_summary(raw) if raw else None
    client = await hd.resolve_client(inc["machine"], th)
    if not client or not client["psa_company_id"]:
        raise CommandError(f"Couldn't match {inc['machine']} to a client with a PSA company — check the client registry")

    comp = await vendor_client.get("connectwise_psa", f"/company/companies/{client['psa_company_id']}")
    site = ((comp["body"] or {}).get("site") or {}).get("id") if comp["ok"] else None
    board_id = inc["tickets"][0]["board_id"]
    closed = await hd.closed_status_id(board_id) if board_id else None
    if not closed:
        raise CommandError("Couldn't find the board's closed status")

    tickets = inc["tickets"]
    title = f"SentinelOne: {(th or {}).get('classification') or 'threat'} {inc['threat_name'] or ''} on {inc['machine']}"
    title = " ".join(title.split())[:100]
    # Follow-up alerts (incident status / verdict changed…) for an incident triaged earlier: merge into that ticket
    existing = await hd.existing_incident_ticket(client["psa_company_id"], inc["machine"], inc["threat_name"],
                                                 {t["id"] for t in tickets})
    if existing:
        parent = {"id": int(existing["id"]), "event": "existing incident ticket",
                  "company_id": client["psa_company_id"], "existing": True}
        children = list(tickets)
    else:
        parent = next((t for t in tickets if "new" in t["event"].lower() and "threat" in t["event"].lower()), tickets[0])
        children = [t for t in tickets if t is not parent]
    closing = bool(th and th["mitigated"])

    steps: list[dict[str, Any]] = []
    for t in ([] if parent.get("existing") else [parent]) + children:
        ops = [{"op": "replace", "path": "company", "value": {"id": client["psa_company_id"]}}]
        if site:
            ops.append({"op": "replace", "path": "site", "value": {"id": site}})
        if t is parent:
            ops.append({"op": "replace", "path": "summary", "value": title})
        if t["company_id"] == client["psa_company_id"] and t is not parent:
            continue
        cur = await vendor_client.get("connectwise_psa", f"/service/tickets/{t['id']}")
        if not cur["ok"] or not isinstance(cur["body"], dict):
            raise CommandError(f"Ticket #{t['id']} not readable")
        if cur["body"].get("closedFlag"):
            raise CommandError(f"Ticket #{t['id']} is already closed")
        step = _http("connectwise_psa", "PATCH", f"/service/tickets/{t['id']}",
                     f"Move ticket #{t['id']} ({t['event']}) to {client['client']}"
                     + (f" and rename to “{title}”" if t is parent else ""), ops,
                     before_ref={"platform": "connectwise_psa", "path": f"/service/tickets/{t['id']}"},
                     compare_keys=["company", "status", "closedFlag"])
        # Stale check: if someone moves/closes the ticket before approval, nothing runs
        step["before"] = {k: cur["body"].get(k) for k in ("company", "status", "closedFlag", "summary")}
        steps.append(step)
    if children:
        steps.append(_http("connectwise_psa", "POST", f"/service/tickets/{parent['id']}/merge",
                           f"Merge #{', #'.join(str(t['id']) for t in children)} into #{parent['id']} (merged tickets → >Closed)",
                           {"mergeTicketIds": [t["id"] for t in children], "status": {"id": closed}}))
    note = hd.ticket_note(inc, th, client, closing)
    steps.append(_http("connectwise_psa", "POST", f"/service/tickets/{parent['id']}/notes",
                       f"Add the incident summary to #{parent['id']} (internal analysis)",
                       {"text": note, "internalAnalysisFlag": True, "detailDescriptionFlag": False}))
    warnings: list[str] = []
    if closing:
        steps.append(_http("sentinelone", "POST", "/web/api/v2.1/threats/notes",
                           f"SentinelOne: note on threat {th['id']} - closed in PSA ticket #{parent['id']}",
                           {"filter": {"ids": [th["id"]]},
                            "data": {"text": f"Closed in ConnectWise PSA ticket #{parent['id']} (help desk triage)."}}))
        if not th["already_resolved"]:
            steps.append(_http("sentinelone", "POST", "/web/api/v2.1/threats/incident",
                               f"SentinelOne: set incident status of threat {th['id']} → Resolved",
                               {"filter": {"ids": [th["id"]]}, "data": {"incidentStatus": "resolved"}}))
        steps.append(_http("connectwise_psa", "PATCH", f"/service/tickets/{parent['id']}",
                           f"Close #{parent['id']} (status >Closed)",
                           [{"op": "replace", "path": "status", "value": {"id": closed}}]))
        steps.append(_http("connectwise_psa", "POST", f"/company/companies/{client['psa_company_id']}/notes",
                           f"Log on {client['client']}: SentinelOne {th['classification']} on {inc['machine']} — resolved",
                           {"text": f"{(th.get('detected') or '')[:10]} SentinelOne {th['classification']} "
                                    f"'{th['name']}' on {inc['machine']} - {th['mitigation_text']}; incident resolved"
                                    + (f"; verdict {th['verdict']}" if th.get("verdict") not in (None, "", "undefined") else "")
                                    + f". Ticket #{parent['id']}."}))
    else:
        why = "SentinelOne threat not found" if not th else \
            f"SentinelOne says “{th['mitigation_text'] or th['mitigation']}” (incident {th['incident']})"
        warnings.append(f"Ticket stays OPEN under {client['client']}: {why}. A technician reviews it in SentinelOne; "
                        "re-run the triage after it's mitigated to close it and log it on the company.")
    if th:
        acts = ", ".join(f"{a['action']} {a['status']}" for a in th["actions"])
        warnings.append(f"Threat {th['name']} — {th['classification']} ({th['confidence']}); actions: {acts}")
    warnings.append(f"Matched {inc['machine']} → {client['client']} ({client['client_code']}) by {client['matched_by']}")
    if parent.get("existing"):
        warnings.insert(0, f"Follow-up alerts for an incident already triaged: merged into existing ticket #{parent['id']}.")
    verb = "Resolve + close" if closing else "Move + merge (stays open)"
    return Plan(f"{verb}: SentinelOne {inc['threat_name'] or 'alert'} on {inc['machine']} — {len(tickets)} ticket(s) → "
                f"{client['client']}" + (f" (into #{parent['id']})" if parent.get("existing") else ""),
                steps, risk="update", client_id=client["client_id"], warnings=warnings)


async def plan_alert_triage(p: dict[str, Any]) -> Plan:
    """Avast / HPE Instant On alerts: customer/site name → PSA company; move, merge repeats, note, close if handled."""
    from app.services import alert_triage as at
    from app.services import helpdesk as hd
    from app.services import vendor_client

    groups = await at.find_groups([int(i) for i in p["ticket_ids"]])
    if not groups:
        raise CommandError("These aren't Avast or HPE Instant On alert tickets (or the customer name is missing)")
    if len(groups) > 1:
        raise CommandError("These tickets are different alerts: " + "; ".join(
            f"{g['source']} {g['name']} {g.get('device', '')} (#{', #'.join(str(t['id']) for t in g['tickets'])})"
            for g in groups) + " — propose each separately")
    g = groups[0]
    if p.get("psa_company_id"):
        cid = int(p["psa_company_id"])
        r = await vendor_client.get("connectwise_psa", f"/company/companies/{cid}")
        if not r["ok"]:
            raise CommandError(f"PSA company {cid} not found")
        match = {"psa_company_id": cid, "company": r["body"].get("name"), "how": "chosen by a person"}
    else:
        match = await at.match_company(g["name"], g["source"])
    if not match["psa_company_id"]:
        cands = ", ".join(c["company"] for c in match.get("candidates", [])[:6]) or "none"
        raise CommandError(f"No confident PSA company match for '{g['name']}' — pick one on the help desk page "
                           f"(closest: {cands})")
    company_id = int(match["psa_company_id"])
    comp = await vendor_client.get("connectwise_psa", f"/company/companies/{company_id}")
    site = ((comp["body"] or {}).get("site") or {}).get("id") if comp["ok"] else None
    board_id = g["tickets"][0]["board_id"]
    closed = await hd.closed_status_id(board_id) if board_id else None
    if not closed:
        raise CommandError("Couldn't find the board's closed status")

    tickets = g["tickets"]
    ids = {t["id"] for t in tickets}
    title = g["title"]
    r = await vendor_client.get("connectwise_psa", "/service/tickets", {
        "conditions": f'closedFlag=false and company/id={company_id} and summary="{title.replace(chr(34), "")}"',
        "pageSize": 5, "orderBy": "id asc"})
    existing = next((t for t in (r["body"] if r["ok"] and isinstance(r["body"], list) else []) if int(t["id"]) not in ids), None)
    if existing:
        parent = {"id": int(existing["id"]), "event": "existing alert ticket", "company_id": company_id, "existing": True}
        children = list(tickets)
    else:
        parent, children = tickets[0], tickets[1:]
    closing = g["source"] == "avast" and g["handled"]

    steps: list[dict[str, Any]] = []
    for t in ([] if parent.get("existing") else [parent]) + children:
        ops = [{"op": "replace", "path": "company", "value": {"id": company_id}}]
        if site:
            ops.append({"op": "replace", "path": "site", "value": {"id": site}})
        if t is parent:
            ops.append({"op": "replace", "path": "summary", "value": title})
        cur = await vendor_client.get("connectwise_psa", f"/service/tickets/{t['id']}")
        if not cur["ok"] or not isinstance(cur["body"], dict) or cur["body"].get("closedFlag"):
            raise CommandError(f"Ticket #{t['id']} is closed or not readable")
        step = _http("connectwise_psa", "PATCH", f"/service/tickets/{t['id']}",
                     f"Move ticket #{t['id']} to {match['company']}" + (f" and rename to “{title}”" if t is parent else ""),
                     ops, before_ref={"platform": "connectwise_psa", "path": f"/service/tickets/{t['id']}"},
                     compare_keys=["company", "status", "closedFlag"])
        step["before"] = {k: cur["body"].get(k) for k in ("company", "status", "closedFlag", "summary")}
        steps.append(step)
    if children:
        steps.append(_http("connectwise_psa", "POST", f"/service/tickets/{parent['id']}/merge",
                           f"Merge #{', #'.join(str(t['id']) for t in children)} into #{parent['id']} (merged → >Closed)",
                           {"mergeTicketIds": [t["id"] for t in children], "status": {"id": closed}}))
    steps.append(_http("connectwise_psa", "POST", f"/service/tickets/{parent['id']}/notes",
                       f"Add the alert summary to #{parent['id']} (internal analysis)",
                       {"text": at.note_text(g, match, closing), "internalAnalysisFlag": True,
                        "detailDescriptionFlag": False}))
    warnings = [f"Matched '{g['name']}' → {match['company']} ({match['how']})"]
    if closing:
        steps.append(_http("connectwise_psa", "PATCH", f"/service/tickets/{parent['id']}",
                           f"Close #{parent['id']} (status >Closed)",
                           [{"op": "replace", "path": "status", "value": {"id": closed}}]))
        steps.append(_http("connectwise_psa", "POST", f"/company/companies/{company_id}/notes",
                           f"Log on {match['company']}: Avast {g['threat']} on {g['device']} - {g['action']}",
                           {"text": f"{g.get('when') or ''} Avast: {g['threat']} ({g.get('severity')}, "
                                    f"{g.get('shield')}) on {g['device']} - {g['action']}. Ticket #{parent['id']}."}))
    else:
        warnings.append("Stays OPEN for a technician: " + (
            "Instant On doesn't report recovery by email." if g["source"] == "instanton"
            else f"Avast action '{g['action'] or 'unknown'}' doesn't show it was handled."))
    if parent.get("existing"):
        warnings.insert(0, f"Repeat alert: merged into existing ticket #{parent['id']}.")
    label = "Avast" if g["source"] == "avast" else "Instant On"
    verb = "Move + close" if closing else "Move (stays open)"
    return Plan(f"{verb}: {label} {g['name']}" + (f" / {g['device']}" if g.get("device") else "")
                + f" — {len(tickets)} ticket(s) → {match['company']}"
                + (f" (into #{parent['id']})" if parent.get("existing") else ""), steps, risk="update",
                warnings=warnings)


register(Command(
    "alert_ticket_triage", "Triage Avast / Instant On alert tickets",
    "For one Avast Business or HPE Instant On alert filed in Catchall: match the customer/site name to its PSA "
    "company, move the ticket(s) there, merge repeats, add a summary; close + log on the company when Avast shows it "
    "handled. Pass psa_company_id to choose the company yourself.",
    "update",
    {"ticket_ids": {"type": "array", "description": "The ticket(s) of ONE alert"},
     "psa_company_id": {"type": "integer", "description": "PSA company to use (when the name doesn't match)"}},
    ["ticket_ids"], plan_alert_triage, ["connectwise_psa"],
))


register(Command(
    "s1_ticket_triage", "Triage SentinelOne alert tickets",
    "For one SentinelOne incident filed in the Catchall company: move its alert tickets to the machine's company, "
    "merge them into one, add the incident summary; if SentinelOne reports it mitigated, resolve it in SentinelOne, "
    "close the ticket and log it on the company.",
    "update",
    {"ticket_ids": {"type": "array", "description": "The alert tickets of ONE incident (see helpdesk_incidents)"}},
    ["ticket_ids"], plan_s1_triage, ["connectwise_psa", "sentinelone"],
))
