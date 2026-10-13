"""Built-in commands: vendor catalog, tickets, naming sync, device cleanup."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from app.db.repository import get_session_factory
from app.db.schema import ApiEndpoint, Client, ClientPlatformLink, PlatformCompany
from app.services import vendor_client
from app.services.client_naming import validate_standard_name
from app.services.client_registry import client_device_index
from app.services.commands import Command, CommandError, Plan, register
from app.services.commands.policy import denied
from app.services.normalizers import hostname_match_key

_S = lambda d, **kw: {"type": "string", "description": d, **kw}  # noqa: E731
_I = lambda d: {"type": "integer", "description": d}  # noqa: E731
_B = lambda d: {"type": "boolean", "description": d}  # noqa: E731
_PLATFORM = _S("Vendor platform", enum=list(vendor_client.PLATFORMS))
_RISK = {"POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete"}


def _http(platform: str, method: str, path: str, description: str, *, body: Any = None,
          params: dict[str, Any] | None = None, before: Any = None, before_path: str | None = None,
          compare_keys: list[str] | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {"kind": "http", "platform": platform, "method": method, "path": path,
                            "params": params or {}, "body": body, "description": description, "before": before}
    if before_path:
        step["before_ref"] = {"platform": platform, "path": before_path, "params": {}}
    if compare_keys:
        step["compare_keys"] = compare_keys
    return step


async def _get_body(platform: str, path: str, params: dict[str, Any] | None = None) -> Any:
    r = await vendor_client.get(platform, path, params)
    return r["body"] if r["ok"] else None


async def _client(client: str) -> Client:
    async with get_session_factory()() as db:
        rows = list((await db.execute(select(Client))).scalars())
    c = next((c for c in rows if c.id == client or (c.client_code or "").upper() == client.upper()), None)
    if not c:
        raise CommandError(f"Client not found: {client}")
    return c


# ── Generic vendor catalog ────────────────────────────────────────────────


def _template_regex(template: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[^}]+\})", template)
    return re.compile("^" + "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts) + "/?$")


async def plan_vendor_call(p: dict[str, Any]) -> Plan:
    platform, method, path = p["platform"], p["method"].upper(), p["path"]
    if method not in _RISK:
        raise CommandError("method must be POST, PUT, PATCH or DELETE (reads don't need approval)")
    if not path.startswith("/") or ".." in path or "?" in path:
        raise CommandError("path must be an absolute API path without query string (use query)")
    rule = denied(platform, path)
    if rule:
        raise CommandError(f"Denied by policy ({rule}). Use a dedicated command or the vendor console.")
    async with get_session_factory()() as db:
        eps = list((await db.execute(
            select(ApiEndpoint).where(ApiEndpoint.platform == platform, ApiEndpoint.method == method)
        )).scalars())
    match = next((e for e in eps if _template_regex(e.path).match(path)), None)
    if not match:
        raise CommandError(f"{method} {path} is not in the {platform} API spec — check /api/discovery/search")
    before = None
    if method in ("PUT", "PATCH", "DELETE"):
        before = await _get_body(platform, path, p.get("query"))
    return Plan(
        title=f"{platform}: {method} {path}",
        risk=_RISK[method],
        steps=[_http(platform, method, path, match.summary or f"{method} {path}", body=p.get("body"),
                     params=p.get("query"), before=before,
                     before_path=path if method in ("PUT", "PATCH", "DELETE") else None)],
        warnings=["Generic catalog call — review the request body carefully."],
    )


register(Command(
    "vendor_call", "Any vendor API write",
    "POST/PUT/PATCH/DELETE any endpoint from a vendor's API spec (policy-filtered).",
    "varies",
    {"platform": _PLATFORM, "method": _S("HTTP method", enum=["POST", "PUT", "PATCH", "DELETE"]),
     "path": _S("Concrete API path, e.g. /company/configurations/4628"),
     "query": {"type": "object", "description": "Query parameters"},
     "body": {"description": "JSON body (object or array; CW PATCH uses an array of operations)"}},
    ["platform", "method", "path"], plan_vendor_call, list(vendor_client.PLATFORMS),
))


# ── Tickets (ConnectWise PSA) ─────────────────────────────────────────────


async def plan_ticket_create(p: dict[str, Any]) -> Plan:
    body: dict[str, Any] = {"summary": p["summary"][:100]}
    if p.get("description"):
        body["initialDescription"] = p["description"]
    for key, field in (("company_id", "company"), ("board_id", "board"), ("priority_id", "priority"),
                       ("status_id", "status")):
        if p.get(key):
            body[field] = {"id": p[key]}
    return Plan(f"Create ticket: {body['summary']}", [
        _http("connectwise_psa", "POST", "/service/tickets", "Create service ticket", body=body)
    ], risk="create")


async def plan_ticket_update(p: dict[str, Any]) -> Plan:
    tid = p["ticket_id"]
    ops = []
    for key, value in p["fields"].items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9/]*", key):
            raise CommandError(f"Invalid field: {key}")
        ops.append({"op": "replace", "path": key, "value": value})
    before = await _get_body("connectwise_psa", f"/service/tickets/{tid}")
    if before is None:
        raise CommandError(f"Ticket {tid} not found")
    keys = [k.split("/")[0] for k in p["fields"]]
    return Plan(f"Update ticket #{tid}: {', '.join(keys)}", [
        _http("connectwise_psa", "PATCH", f"/service/tickets/{tid}", f"Update {', '.join(keys)}", body=ops,
              before={k: before.get(k) for k in keys}, before_path=f"/service/tickets/{tid}", compare_keys=keys)
    ])


async def plan_ticket_note(p: dict[str, Any]) -> Plan:
    internal = bool(p.get("internal", True))
    body = {"text": p["text"], "internalAnalysisFlag": internal, "detailDescriptionFlag": not internal}
    return Plan(f"Add {'internal' if internal else 'customer-visible'} note to ticket #{p['ticket_id']}", [
        _http("connectwise_psa", "POST", f"/service/tickets/{p['ticket_id']}/notes", "Add note", body=body)
    ], risk="create")


async def plan_ticket_time(p: dict[str, Any]) -> Plan:
    body: dict[str, Any] = {"chargeToId": p["ticket_id"], "chargeToType": "ServiceTicket",
                            "timeStart": p["time_start"], "timeEnd": p["time_end"]}
    if p.get("notes"):
        body["notes"] = p["notes"]
    if p.get("member_identifier"):
        body["member"] = {"identifier": p["member_identifier"]}
    return Plan(f"Add time to ticket #{p['ticket_id']}", [
        _http("connectwise_psa", "POST", "/time/entries", "Add time entry", body=body)
    ], risk="create")


_TICKET = _I("PSA ticket number")
register(Command("ticket_create", "Create PSA ticket", "Create a ConnectWise service ticket.", "create",
                 {"summary": _S("Summary (max 100)"), "description": _S("Initial description"),
                  "company_id": _I("PSA company id (get_client → psa_company_id)"), "board_id": _I("Board id"),
                  "priority_id": _I("Priority id"), "status_id": _I("Status id")},
                 ["summary"], plan_ticket_create, ["connectwise_psa"]))
register(Command("ticket_update", "Update PSA ticket", "Change ticket fields (status, priority, board, summary…).",
                 "update", {"ticket_id": _TICKET, "fields": {"type": "object",
                            "description": "Field → value, e.g. {\"status\": {\"id\": 123}, \"summary\": \"…\"}"}},
                 ["ticket_id", "fields"], plan_ticket_update, ["connectwise_psa"]))
register(Command("ticket_add_note", "Add PSA ticket note", "Add an internal (default) or customer-visible note.",
                 "create", {"ticket_id": _TICKET, "text": _S("Note text"), "internal": _B("Internal note (default true)")},
                 ["ticket_id", "text"], plan_ticket_note, ["connectwise_psa"]))
register(Command("ticket_add_time", "Add PSA time entry", "Log time against a ticket.", "create",
                 {"ticket_id": _TICKET, "time_start": _S("ISO start, e.g. 2026-10-10T14:00:00Z"),
                  "time_end": _S("ISO end"), "notes": _S("Work notes"),
                  "member_identifier": _S("PSA member identifier (default: API member)")},
                 ["ticket_id", "time_start", "time_end"], plan_ticket_time, ["connectwise_psa"]))


# ── Device cleanup ────────────────────────────────────────────────────────


_INACTIVE_STATUS: dict[str, Any] = {}


async def _psa_inactive_status() -> dict[str, Any]:
    """The PSA configuration status named 'Inactive' (activeFlag is derived from status and can't be set)."""
    if not _INACTIVE_STATUS:
        rows = await _get_body("connectwise_psa", "/company/configurations/statuses") or []
        match = next((s for s in rows if str(s.get("description", "")).strip().lower() == "inactive"), None) \
            or next((s for s in rows if s.get("closedFlag")), None)
        if not match:
            raise CommandError("No 'Inactive' configuration status found in PSA")
        _INACTIVE_STATUS.update(id=match["id"], name=match.get("description"))
    return dict(_INACTIVE_STATUS)


async def plan_psa_inactivate(p: dict[str, Any]) -> Plan:
    cid = p["configuration_id"]
    before = await _get_body("connectwise_psa", f"/company/configurations/{cid}")
    if before is None:
        raise CommandError(f"Configuration {cid} not found")
    inactive = await _psa_inactive_status()
    current = (before.get("status") or {})
    if current.get("id") == inactive["id"]:
        raise CommandError(f"{before.get('name')} ({cid}) is already {inactive['name']}")
    return Plan(f"Inactivate PSA configuration {before.get('name')} ({cid})", [
        _http("connectwise_psa", "PATCH", f"/company/configurations/{cid}",
              f"Set status {current.get('name')} → {inactive['name']} on {before.get('name')} "
              f"({(before.get('company') or {}).get('name')})",
              body=[{"op": "replace", "path": "status", "value": {"id": inactive["id"]}}],
              before={"name": before.get("name"), "status": {"id": current.get("id"), "name": current.get("name")},
                      "serialNumber": before.get("serialNumber")},
              before_path=f"/company/configurations/{cid}", compare_keys=["name", "status"])
    ], risk="update")


async def _psa_endpoint_step(cid: int, endpoint_id: str) -> dict[str, Any]:
    cfg = await _get_body("connectwise_psa", f"/company/configurations/{cid}")
    if cfg is None:
        raise CommandError(f"Configuration {cid} not found")
    fields = cfg.get("customFields") or []
    target = next((f for f in fields if str(f.get("caption", "")).strip().lower() == "endpoint id"), None)
    if target is None:
        raise CommandError(f"Configuration {cid} has no 'Endpoint ID' custom field")
    old = target.get("value")
    new_fields = [dict(f, value=endpoint_id) if f is target else f for f in fields]
    return _http("connectwise_psa", "PATCH", f"/company/configurations/{cid}",
                 f"Set Endpoint ID on {cfg.get('name')}: {old or '(empty)'} → {endpoint_id}",
                 body=[{"op": "replace", "path": "customFields", "value": new_fields}],
                 before={"name": cfg.get("name"), "endpoint_id": old},
                 before_path=f"/company/configurations/{cid}", compare_keys=["name"])


async def plan_psa_set_endpoint(p: dict[str, Any]) -> Plan:
    step = await _psa_endpoint_step(p["configuration_id"], p["endpoint_id"])
    return Plan(f"Set PSA Endpoint ID on configuration {p['configuration_id']}", [step])


async def plan_s1_decommission(p: dict[str, Any]) -> Plan:
    from app.services.commands.actions import READERS
    ids = [str(i) for i in p["agent_ids"]]
    before = await READERS["s1_read_agents"]({"agent_ids": ids})
    if not before:
        raise CommandError("None of those SentinelOne agents were found")
    names = ", ".join(a["computerName"] or a["id"] for a in before)
    return Plan(f"Decommission SentinelOne agent(s): {names}", [{
        "kind": "action", "action": "s1_decommission", "args": {"agent_ids": ids},
        "description": f"Decommission {names} (removes from console, uninstalls if reachable)",
        "before": before, "before_ref": {"action": "s1_read_agents", "args": {"agent_ids": ids}},
    }], risk="delete")


async def plan_asio_remove(p: dict[str, Any]) -> Plan:
    return Plan(f"Remove Asio endpoint {p['endpoint_id']}", [{
        "kind": "action", "action": "asio_remove_endpoint",
        "args": {"endpoint_id": p["endpoint_id"], "reason": p.get("reason") or "Decommissioned via MSP Tools change"},
        "description": f"Uninstall/remove Asio endpoint {p['endpoint_id']}", "before": None,
    }], risk="delete")


register(Command("psa_inactivate_configuration", "Inactivate PSA configuration",
                 "Mark a PSA configuration inactive (never deleted — keeps history).", "update",
                 {"configuration_id": _I("PSA configuration id")}, ["configuration_id"],
                 plan_psa_inactivate, ["connectwise_psa"]))
register(Command("psa_set_endpoint_id", "Set PSA Endpoint ID",
                 "Write the Asio endpoint UUID into the PSA configuration's 'Endpoint ID' field.", "update",
                 {"configuration_id": _I("PSA configuration id"), "endpoint_id": _S("Asio endpoint UUID")},
                 ["configuration_id", "endpoint_id"], plan_psa_set_endpoint, ["connectwise_psa"]))
register(Command("s1_decommission_agent", "Decommission SentinelOne agent(s)",
                 "Remove agents from SentinelOne (and uninstall if online).", "delete",
                 {"agent_ids": {"type": "array", "description": "SentinelOne agent ids"}}, ["agent_ids"],
                 plan_s1_decommission, ["sentinelone"]))
register(Command("asio_remove_endpoint", "Remove Asio endpoint", "Uninstall/remove an Asio RMM endpoint.", "delete",
                 {"endpoint_id": _S("Asio endpoint UUID"), "reason": _S("Reason recorded in Asio")},
                 ["endpoint_id"], plan_asio_remove, ["asio"]))


async def plan_offboard_device(p: dict[str, Any]) -> Plan:
    """Retire one machine everywhere (device naming standard / offboarding procedure): S1 decommission, Asio remove, PSA inactivate."""
    key = hostname_match_key(p["hostname"].upper())[:15]
    async with get_session_factory()() as db:
        devices = [d for d in await client_device_index(db) if d["host_key"] == key]
    if not devices:
        raise CommandError(f"No stored device matches hostname {p['hostname']}")
    clients = {d["client_id"] for d in devices if d["client_id"]}
    if len(clients) > 1:
        raise CommandError("That hostname exists under more than one client — use the individual commands")
    steps: list[dict[str, Any]] = []
    warnings: list[str] = []
    s1 = [d["platform_id"] for d in devices if d["platform"] == "sentinelone"]
    if s1:
        steps += (await plan_s1_decommission({"agent_ids": s1})).steps
    for d in devices:
        if d["platform"] == "asio":
            steps += (await plan_asio_remove({"endpoint_id": d["endpoint_id"] or d["platform_id"],
                                              "reason": f"Offboarded {p['hostname']}"})).steps
    for d in devices:
        if d["platform"] == "connectwise_psa":
            try:
                steps += (await plan_psa_inactivate({"configuration_id": int(d["platform_id"])})).steps
            except CommandError as exc:
                warnings.append(str(exc))
    if any(d["platform"] == "screenconnect" for d in devices):
        warnings.append("ScreenConnect session must be removed in the ScreenConnect console (no API delete).")
    if not steps:
        raise CommandError("Nothing to do for that hostname")
    return Plan(f"Offboard device {p['hostname']}", steps, risk="delete",
                client_id=next(iter(clients), None), warnings=warnings)


async def plan_fix_endpoint_ids(p: dict[str, Any]) -> Plan:
    """For a client's PSA configurations missing Endpoint ID, fill it from the Asio device with the same hostname."""
    c = await _client(p["client"])
    async with get_session_factory()() as db:
        devices = [d for d in await client_device_index(db) if d["client_id"] == c.id]
    asio = {d["host_key"]: (d["endpoint_id"] or d["platform_id"]) for d in devices if d["platform"] == "asio"}
    psa_count: dict[str, int] = {}
    for d in devices:
        if d["platform"] == "connectwise_psa" and d["host_key"]:
            psa_count[d["host_key"]] = psa_count.get(d["host_key"], 0) + 1
    steps, warnings = [], []
    for d in devices:
        if d["platform"] != "connectwise_psa" or d["endpoint_id"] or d["host_key"] not in asio:
            continue
        if psa_count.get(d["host_key"], 0) > 1:
            warnings.append(f"{d['hostname']}: {psa_count[d['host_key']]} PSA configurations — inactivate the "
                            f"duplicate (psa_inactivate_configuration) before setting the Endpoint ID.")
            psa_count[d["host_key"]] = 0  # warn once
            continue
        if psa_count.get(d["host_key"]) == 0:
            continue
        try:
            steps.append(await _psa_endpoint_step(int(d["platform_id"]), asio[d["host_key"]]))
        except CommandError as exc:
            warnings.append(str(exc))
        if len(steps) >= int(p.get("limit", 50)):
            warnings.append("Limit reached — run again for the rest.")
            break
    if not steps:
        raise CommandError("No PSA configurations can safely get an Endpoint ID right now. "
                           + (" ".join(warnings) if warnings else "None are missing one with a matching Asio device."))
    return Plan(f"Fill PSA Endpoint IDs for {c.standard_name} ({len(steps)})", steps, client_id=c.id,
                warnings=warnings)


async def plan_psa_duplicates(p: dict[str, Any]) -> Plan:
    """Inactivate duplicate PSA configurations: keep the one linked to Asio by Endpoint ID, inactivate the rest."""
    c = await _client(p["client"])
    async with get_session_factory()() as db:
        devices = [d for d in await client_device_index(db) if d["client_id"] == c.id]
    asio_eps = {(d["endpoint_id"] or d["platform_id"]).lower() for d in devices if d["platform"] == "asio"}
    groups: dict[str, list[dict[str, Any]]] = {}
    for d in devices:
        if d["platform"] == "connectwise_psa" and d["host_key"]:
            groups.setdefault(d["host_key"], []).append(d)
    steps, warnings = [], []
    for rows in groups.values():
        if len(rows) < 2:
            continue
        linked = [r for r in rows if r["endpoint_id"] and r["endpoint_id"].lower() in asio_eps]
        if len(linked) != 1:
            warnings.append(f"{rows[0]['hostname']}: {len(rows)} PSA configurations but "
                            f"{'none' if not linked else 'several'} linked to Asio — resolve manually.")
            continue
        for r in rows:
            if r is linked[0]:
                continue
            try:
                step = (await plan_psa_inactivate({"configuration_id": int(r["platform_id"])})).steps[0]
                step["description"] += f" — duplicate of configuration {linked[0]['platform_id']} (linked to Asio)"
                steps.append(step)
            except CommandError as exc:
                warnings.append(str(exc))
        if len(steps) >= int(p.get("limit", 50)):
            warnings.append("Limit reached — run again for the rest.")
            break
    if not steps:
        raise CommandError("No duplicate PSA configurations can be resolved automatically. " + " ".join(warnings))
    return Plan(f"Inactivate duplicate PSA configurations for {c.standard_name} ({len(steps)})", steps,
                client_id=c.id, warnings=warnings)


register(Command("psa_inactivate_duplicates", "Inactivate duplicate PSA configurations",
                 "For hostnames with several PSA configurations, keep the one linked to Asio by Endpoint ID and "
                 "inactivate the others (kept for history).", "update",
                 {"client": _S("Client id or code"), "limit": _I("Max configurations (default 50)")},
                 ["client"], plan_psa_duplicates, ["connectwise_psa"]))
register(Command("offboard_device", "Offboard a device everywhere",
                 "SentinelOne decommission + Asio remove + PSA inactivate for one hostname.", "delete",
                 {"hostname": _S("Device hostname")}, ["hostname"], plan_offboard_device,
                 ["sentinelone", "asio", "connectwise_psa"]))
register(Command("fix_psa_endpoint_ids", "Fill missing PSA Endpoint IDs",
                 "Match a client's PSA configurations to Asio endpoints by hostname and fill 'Endpoint ID'.",
                 "update", {"client": _S("Client id or code"), "limit": _I("Max configurations (default 50)")},
                 ["client"], plan_fix_endpoint_ids, ["connectwise_psa"]))


# ── Naming sync ─────────────────────────────────────────────────


async def plan_apply_standard_name(p: dict[str, Any]) -> Plan:
    c = await _client(p["client"])
    if c.naming_status != "approved":
        raise CommandError(f"{c.standard_name}: approve the standard name first (client registry naming_status)")
    problems = validate_standard_name(c.standard_name)
    if problems:
        raise CommandError(f"Standard name breaks the naming standard: {', '.join(problems)}")
    std = c.standard_name
    async with get_session_factory()() as db:
        links = list((await db.execute(select(ClientPlatformLink).where(
            ClientPlatformLink.client_id == c.id, ClientPlatformLink.status == "confirmed"))).scalars())
        pcs = {(pc.platform, pc.platform_company_id): pc
               for pc in (await db.execute(select(PlatformCompany))).scalars()}
    steps: list[dict[str, Any]] = []
    warnings: list[str] = []
    for ln in links:
        pc = pcs.get((ln.platform, ln.platform_company_id))
        current = pc.name if pc else ""
        if current.strip().casefold() == std.casefold():
            continue
        if ln.platform == "connectwise_psa":
            steps.append(_http("connectwise_psa", "PATCH", f"/company/companies/{ln.platform_company_id}",
                               f"PSA company name: {current} → {std}",
                               body=[{"op": "replace", "path": "name", "value": std}],
                               before={"name": current}, before_path=f"/company/companies/{ln.platform_company_id}",
                               compare_keys=["name"]))
        elif ln.platform == "sentinelone":
            steps.append(_http("sentinelone", "PUT", f"/web/api/v2.1/sites/{ln.platform_company_id}",
                               f"SentinelOne site name: {current} → {std}", body={"data": {"name": std}},
                               before={"name": current}))
        elif ln.platform == "screenconnect":
            sc = vendor_client._connector("screenconnect")  # noqa: SLF001
            sessions = [s for s in await sc._fetch_sessions()  # noqa: SLF001
                        if ((s.get("CustomPropertyValues") or [""])[0] or "").strip() == ln.platform_company_id]
            for s in sessions:
                props = list(s.get("CustomPropertyValues") or [])
                props += [""] * (8 - len(props))
                new = [std] + props[1:8]
                steps.append({
                    "kind": "action", "action": "sc_update_custom_properties",
                    "args": {"session_id": s.get("SessionID"), "properties": new},
                    "description": f"ScreenConnect {s.get('Name')}: Company {props[0]} → {std}",
                    "before": {"name": s.get("Name"), "custom_properties": props[:8]},
                    "before_ref": {"action": "sc_read_session", "args": {"session_id": s.get("SessionID")}},
                })
        else:
            warnings.append(f"{ln.platform}: rename '{current}' → '{std}' isn't automated yet "
                            f"(Asio API scope / ESET API) — rename in the console or use vendor_call.")
    if not steps:
        raise CommandError(f"{std}: every automatable tool already uses the standard name")
    return Plan(f"Apply standard name '{std}' ({c.client_code})", steps, client_id=c.id, warnings=warnings)


register(Command("apply_standard_name", "Apply standard client name",
                 "Rename the client in PSA, SentinelOne and every ScreenConnect session to its approved standard name.",
                 "update", {"client": _S("Client id or code")}, ["client"], plan_apply_standard_name,
                 ["connectwise_psa", "sentinelone", "screenconnect"]))
