"""Client offboarding — delete a client that has no history (test companies, onboarding mistakes).

Refuses to plan when PSA shows tickets, time, invoices, configurations or projects for the company, and
re-checks all of that right before anything is deleted. Deletes the PSA agreements (additions first), the
company and its contacts, the SentinelOne site (only if it has no agents) or the ESET company (suspend, then
delete), and the registry client.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.services import vendor_client
from app.services.commands import Command, CommandError, Plan, register

# PSA records that mean the company has history and must not be deleted.
_HISTORY = (
    ("/service/tickets", "tickets"),
    ("/time/entries", "time entries"),
    ("/finance/invoices", "invoices"),
    ("/company/configurations", "configurations"),
    ("/project/projects", "projects"),
)


def _step(platform: str, method: str, path: str, description: str, body: Any = None,
          params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"kind": "http", "platform": platform, "method": method, "path": path, "params": params or {},
            "body": body, "description": description, "before": None}


async def _psa_list(path: str, conditions: str) -> list[dict[str, Any]]:
    r = await vendor_client.get("connectwise_psa", path, {"conditions": conditions, "pageSize": 100})
    if not r["ok"]:
        raise CommandError(f"PSA {path} not readable (HTTP {r['status']})")
    return r["body"] if isinstance(r["body"], list) else []


async def plan_offboard_client(p: dict[str, Any]) -> Plan:
    from app.db.repository import get_session_factory
    from app.db.schema import Client, ClientPlatformLink

    code = p["client_code"].strip().upper()
    async with get_session_factory()() as db:
        client = (await db.execute(select(Client).where(Client.client_code == code))).scalar_one_or_none()
        if client is None:
            raise CommandError(f"No client with code {code} in the registry")
        links = (await db.execute(select(ClientPlatformLink).where(
            ClientPlatformLink.client_id == client.id, ClientPlatformLink.status != "rejected"))).scalars().all()
    by_platform: dict[str, list[str]] = {}
    for link in links:
        by_platform.setdefault(link.platform, []).append(link.platform_company_id)
    psa_id = client.psa_company_id or next(iter(by_platform.get("connectwise_psa", [])), None)
    steps: list[dict[str, Any]] = []
    warnings: list[str] = []
    company: dict[str, Any] | None = None
    if psa_id:
        psa_id = int(psa_id)
        r = await vendor_client.get("connectwise_psa", f"/company/companies/{psa_id}")
        if r["ok"] and isinstance(r["body"], dict) and not r["body"].get("deletedFlag"):
            company = r["body"]
        elif r["status"] not in (404,) and not (r["ok"] and isinstance(r["body"], dict)):
            raise CommandError(f"PSA company {psa_id} not readable (HTTP {r['status']})")
    if company is None:
        warnings.append("PSA company is already deleted (or missing) — PSA steps skipped.")
    elif str(company.get("identifier", "")).upper() != code:
        raise CommandError(f"PSA company {psa_id} has Company ID {company.get('identifier')!r}, not {code} — refusing")

    if company is not None:
        history = []
        for path, label in _HISTORY:
            rows = await _psa_list(path, f"company/id={psa_id}")
            if rows:
                history.append(f"{len(rows)}{'+' if len(rows) == 100 else ''} {label}")
        if history:
            raise CommandError(f"{company['name']} has history in PSA ({', '.join(history)}) — it can't be "
                               "deleted. Inactivate it instead.")
        # Pre-checks, re-run right before anything is deleted
        for path, label in _HISTORY:
            steps.append({"kind": "assert", "platform": "connectwise_psa", "method": "GET", "path": path,
                          "params": {"conditions": f"company/id={psa_id}", "pageSize": 5}, "expect": "empty",
                          "description": f"PSA company has no {label}", "before": None})
    s1_sites = []
    for site_id in by_platform.get("sentinelone", []):
        r = await vendor_client.get("sentinelone", "/web/api/v2.1/sites", {"siteIds": site_id})
        if r["ok"] and ((r["body"] or {}).get("data") or {}).get("sites"):
            s1_sites.append(site_id)
        else:
            warnings.append(f"SentinelOne site {site_id} not found — skipped.")
    for site_id in s1_sites:
        steps.append({"kind": "assert", "platform": "sentinelone", "method": "GET", "path": "/web/api/v2.1/agents",
                      "params": {"siteIds": site_id, "limit": 5}, "expect": "empty",
                      "description": f"SentinelOne site {site_id} has no agents", "before": None})

    if company is not None:
        # PSA agreements (additions first), company, contacts
        for agr in await _psa_list("/finance/agreements", f"company/id={psa_id}"):
            for add in await _psa_list(f"/finance/agreements/{agr['id']}/additions", "id>0"):
                steps.append(_step("connectwise_psa", "DELETE",
                                   f"/finance/agreements/{agr['id']}/additions/{add['id']}",
                                   f"Delete addition {(add.get('product') or {}).get('identifier')} × "
                                   f"{add.get('quantity'):g} from '{agr['name']}'"))
            steps.append(_step("connectwise_psa", "DELETE", f"/finance/agreements/{agr['id']}",
                               f"Delete agreement '{agr['name']}'"))
        contacts = await _psa_list("/company/contacts", f"company/id={psa_id}")
        steps.append(_step("connectwise_psa", "DELETE", f"/company/companies/{psa_id}",
                           f"Delete PSA company '{company['name']}' (Company ID {code})"))
        steps.append({"kind": "local", "action": "psa_delete_company_contacts",
                      "description": f"Delete remaining PSA contacts ({len(contacts)}: "
                                     + ", ".join(f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
                                                 for c in contacts[:5])
                                     + ") — PSA keeps the default contact hidden with the deleted company",
                      "args": {"psa_company_id": psa_id}, "before": None})

    # Antivirus
    for site_id in s1_sites:
        steps.append(_step("sentinelone", "DELETE", f"/web/api/v2.1/sites/{site_id}",
                           f"Delete SentinelOne site {site_id}"))
    eset_ids = set()
    if by_platform.get("eset"):
        try:
            eset_ids = {r.platform_id for r in await vendor_client._connector("eset").get_companies()}  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"ESET not readable: {exc}") from exc
    for eset_id in by_platform.get("eset", []):
        if eset_id not in eset_ids:
            warnings.append(f"ESET company {eset_id} is already gone — skipped.")
            continue
        steps.append({"kind": "local", "action": "eset_suspend_then_delete", "slow": True,
                      "description": f"Suspend ESET company {eset_id}, wait until suspended (1–3 min), then delete it",
                      "args": {"company_id": eset_id}, "before": None})

    steps.append({"kind": "local", "action": "registry_delete_client",
                  "description": f"Remove {client.standard_name} ({code}) from the client registry",
                  "args": {"client_code": code}, "before": None})

    if by_platform.get("asio"):
        warnings.append("Asio company is linked — remove it in Asio (or let the ConnectWise integration drop it).")
    if by_platform.get("screenconnect"):
        warnings.append("ScreenConnect sessions are linked — uninstall/remove them in ScreenConnect.")
    return Plan(f"Delete client {client.standard_name} ({code})", steps, risk="delete",
                client_id=client.id, warnings=warnings)


register(Command(
    "offboard_client", "Delete a client (no history)",
    "Delete a client that has no PSA history (tickets, time, invoices, configurations, projects): PSA agreements, "
    "company and contacts, SentinelOne site (no agents) or ESET company, and the registry entry. For test "
    "companies and onboarding mistakes — real clients with history must be inactivated instead.",
    "delete",
    {"client_code": {"type": "string", "description": "Registry client code (= PSA Company ID)"}},
    ["client_code"],
    plan_offboard_client, ["connectwise_psa", "sentinelone", "eset"],
))
