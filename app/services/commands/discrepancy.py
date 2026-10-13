"""Discrepancy fixes (change requests): inactivate PSA configurations that no longer match a machine.

``psa_inactivate_orphans`` — per client: active PSA configurations with no machine in Asio/ScreenConnect/
SentinelOne, plus the extra configurations where one machine has several. Each step is the existing guarded
``psa_inactivate_configuration`` update (status → Inactive, kept for history, stale-checked).
"""

from __future__ import annotations

from typing import Any

from app.services.commands import Command, CommandError, Plan, register


async def plan_inactivate_orphans(p: dict[str, Any]) -> Plan:
    from app.db.repository import get_session_factory
    from app.services.commands.builtin import plan_psa_inactivate
    from app.services.discrepancies import build_report

    code = p["client_code"].strip().upper()
    async with get_session_factory()() as db:
        report = await build_report(db, code)
    entry = next((c for c in report["clients"] if (c["client_code"] or "").upper() == code), None)
    if entry is None:
        raise CommandError(f"No client {code} with devices")
    issues = entry["issues"] or {}
    candidates: dict[str, str] = {}  # config id → why
    if p.get("include_orphans", True):
        for m in issues.get("psa_without_machine", []):
            candidates[str(m["psa"]["platform_id"])] = f"{m['hostname']}: no machine in Asio/ScreenConnect/S1"
    if p.get("include_duplicates", True):
        for m in issues.get("psa_duplicate", []):
            for x in m["extra"]:
                candidates[str(x["platform_id"])] = f"{m['hostname']}: duplicate (keeping config {m['keep']['platform_id']})"
    if p.get("configuration_ids"):
        wanted = {str(i) for i in p["configuration_ids"]}
        unknown = wanted - set(candidates)
        if unknown:
            raise CommandError(f"Not an orphan/duplicate for {code}: {', '.join(sorted(unknown))}")
        candidates = {k: v for k, v in candidates.items() if k in wanted}
    if not candidates:
        raise CommandError(f"{entry['client']} has no orphaned or duplicate active PSA configurations")
    steps: list[dict[str, Any]] = []
    skipped: list[str] = []
    for cid, why in candidates.items():
        try:
            sub = await plan_psa_inactivate({"configuration_id": int(cid)})
        except CommandError as exc:
            skipped.append(f"{cid}: {exc}")
            continue
        for s in sub.steps:
            s["description"] += f" — {why}"
        steps += sub.steps
    if not steps:
        raise CommandError("Nothing to inactivate: " + "; ".join(skipped))
    return Plan(f"Inactivate {len(steps)} orphaned/duplicate PSA configuration(s) at {entry['client']} ({code})",
                steps, risk="update", client_id=entry["client_id"],
                warnings=["Configurations are set to Inactive (kept for history), never deleted."] + skipped)


register(Command(
    "psa_inactivate_orphans", "Inactivate orphaned PSA configurations",
    "Per client: set to Inactive the active PSA configurations that match no machine in Asio, ScreenConnect or "
    "SentinelOne, and the extra configurations where one machine has several (from the discrepancy report).",
    "update",
    {"client_code": {"type": "string", "description": "Client code"},
     "configuration_ids": {"type": "array", "description": "Only these PSA configuration ids (default: all found)"},
     "include_orphans": {"type": "boolean", "description": "Configs with no machine (default true)"},
     "include_duplicates": {"type": "boolean", "description": "Extra configs for one machine (default true)"}},
    ["client_code"], plan_inactivate_orphans, ["connectwise_psa"],
))
