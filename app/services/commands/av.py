"""Antivirus gap commands — every step is a change request (an Editor submits, an Approver approves).

* ``av_flag_for_review`` — put machines missing their antivirus into the Asio review group (custom field), for one
  client, several, or all at once.
* ``av_remediate`` — per client: flag + run the install script through Asio (SentinelOne with the client's site
  token, ESET with that company's Live Installer). The next AV sweep verifies each machine and clears its flag.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.services.commands import Command, CommandError, Plan, register

_FIXABLE = ("no_antivirus", "wrong_antivirus")


async def _clients_by_code(db: Any, codes: list[str] | None) -> dict[str, Any]:
    from app.db.schema import Client
    rows = list((await db.execute(select(Client))).scalars())
    if not codes:
        return {c.id: c for c in rows}
    wanted = {c.strip().upper() for c in codes if c.strip()}
    found = {c.id: c for c in rows if (c.client_code or "").upper() in wanted}
    missing = wanted - {(c.client_code or "").upper() for c in found.values()}
    if missing:
        raise CommandError(f"Unknown client code(s): {', '.join(sorted(missing))}")
    return found


def _machines(entry: dict[str, Any], *, include_flagged: bool) -> list[dict[str, Any]]:
    return [m for m in entry["machines"] if m["status"] in _FIXABLE
            and (include_flagged or not m["review"])]


def _flag_step(entry: dict[str, Any], machines: list[dict[str, Any]]) -> dict[str, Any]:
    from app.services.av_coverage import FLAG_FIELD, FLAG_VALUE
    names = ", ".join(m["hostname"] for m in machines[:8]) + (" …" if len(machines) > 8 else "")
    return {"kind": "action", "action": "asio_set_av_flag",
            "description": f"{entry['client']} ({entry['client_code']}): set Asio '{FLAG_FIELD}' = '{FLAG_VALUE}' on "
                           f"{len(machines)} machine(s) → review group: {names}",
            "args": {"endpoint_ids": [m["endpoint_id"] for m in machines],
                     "hostnames": [m["hostname"] for m in machines], "client_id": entry["client_id"],
                     "expected_av": entry["av_vendor"], "value": FLAG_VALUE},
            "before": [{"hostname": m["hostname"], "status": m["status"], "found": m["found"]} for m in machines]}


async def plan_flag(p: dict[str, Any]) -> Plan:
    from app.db.repository import get_session_factory
    from app.services.av_coverage import compute_gaps

    async with get_session_factory()() as db:
        clients = await _clients_by_code(db, p.get("client_codes"))
        gaps = await compute_gaps(db)
    if not gaps["swept_at"]:
        raise CommandError("No AV sweep yet — run 'Refresh AV status' first")
    steps = []
    for entry in gaps["clients"]:
        if entry["client_id"] not in clients:
            continue
        machines = _machines(entry, include_flagged=False)
        if machines:
            steps.append(_flag_step(entry, machines))
    if not steps:
        raise CommandError("Nothing to flag — every gap machine is already flagged, or there are no gaps")
    total = sum(len(s["args"]["endpoint_ids"]) for s in steps)
    return Plan(f"Flag {total} machine(s) without antivirus for review ({len(steps)} client(s))", steps,
                risk="update", warnings=[f"Data from the AV sweep at {gaps['swept_at']}. Stale machines (no Asio "
                                         "contact in 30+ days) are not flagged."])


async def plan_remediate(p: dict[str, Any]) -> Plan:
    from app.db.repository import get_session_factory
    from app.services.av_coverage import compute_gaps, deployment_for_client

    code = p["client_code"].strip().upper()
    async with get_session_factory()() as db:
        client = next(iter((await _clients_by_code(db, [code])).values()))
        gaps = await compute_gaps(db, client.id)
        entry = next((e for e in gaps["clients"] if e["client_id"] == client.id), None)
        if not gaps["swept_at"]:
            raise CommandError("No AV sweep yet — run 'Refresh AV status' first")
        if not entry or not _machines(entry, include_flagged=True):
            raise CommandError(f"{client.standard_name} has no machines missing {client.av_vendor} in the last sweep")
        deploy = await deployment_for_client(db, client)
    if not deploy["ready"]:
        raise CommandError("Can't deploy yet: " + " ".join(deploy["problems"]))
    machines = _machines(entry, include_flagged=True)
    if p.get("endpoint_ids"):
        wanted = set(p["endpoint_ids"])
        machines = [m for m in machines if m["endpoint_id"] in wanted]
        if not machines:
            raise CommandError("None of the given endpoint ids are gap machines for this client")
    unflagged = [m for m in machines if not m["review"]]
    steps = [_flag_step(entry, unflagged)] if unflagged else []
    params = deploy["parameters"]
    shown = {k: (v[:6] + "…" + v[-4:] if k == "SITETOKEN" and len(v) > 12 else v) for k, v in params.items()}
    steps.append({"kind": "action", "action": "asio_run_av_script",
                  "description": f"Run Asio script '{deploy['script']}' on {len(machines)} machine(s) to install "
                                 f"{'SentinelOne' if client.av_vendor == 'sentinelone' else 'ESET'} — {shown}",
                  "args": {"script_name": deploy["script"], "company_id": deploy["asio_company_id"],
                           "endpoint_ids": [m["endpoint_id"] for m in machines],
                           "hostnames": [m["hostname"] for m in machines], "client_id": client.id,
                           "expected_av": client.av_vendor, "parameters": params},
                  "before": [{"hostname": m["hostname"], "status": m["status"], "found": m["found"],
                              "online": m["online"], "last_contact": m["last_contact"]} for m in machines]})
    offline = [m["hostname"] for m in machines if m["online"] is False]
    warnings = ["After it runs, the next AV sweep checks each machine: antivirus found → its Asio flag is cleared; "
                "still missing after 2 sweeps → marked not fixed on this change."]
    if offline:
        warnings.append(f"Offline in Asio right now (the install runs when they come online, if Asio queues it): "
                        f"{', '.join(offline[:10])}")
    if deploy.get("installer"):
        warnings.append(f"ESET installer: {deploy['installer'].get('display_name')} "
                        f"({deploy['installer'].get('create_time')})")
    return Plan(f"Redeploy {client.av_vendor} on {len(machines)} machine(s) at {client.standard_name} ({code})",
                steps, risk="update", client_id=client.id, warnings=warnings)


register(Command(
    "av_flag_for_review", "Flag machines without antivirus for review",
    "Put machines missing their client's antivirus into the Asio review group (custom field 'AV Status'). "
    "One client, several, or all (omit client_codes).",
    "update",
    {"client_codes": {"type": "array", "description": "Client codes to include (omit = every client with gaps)"}},
    [], plan_flag, ["asio"],
))
register(Command(
    "av_remediate", "Redeploy antivirus for one client",
    "Per client: flag the machines missing their antivirus and run the install through Asio — SentinelOne with the "
    "client's site token, ESET with that company's Live Installer. Verified and unflagged by the next AV sweep.",
    "update",
    {"client_code": {"type": "string", "description": "Client code"},
     "endpoint_ids": {"type": "array", "description": "Only these machines (default: every gap machine)"}},
    ["client_code"], plan_remediate, ["asio", "sentinelone", "eset"],
))
