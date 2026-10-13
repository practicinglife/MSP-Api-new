"""New client onboarding: one change that creates the client correctly in PSA + the antivirus platform.

PSA first (company, Main site, main contact as default, managed-services agreement with additions), then the
registry client, then SentinelOne site or ESET company with the MSP alert mailbox as the contact. Asio arrives through the
ConnectWise integration; ScreenConnect values are set by the installer.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import httpx

from app.org_profile import ALERT_EMAIL, PSA_AGREEMENT_LABEL, PSA_PRODUCT_LINES
from app.org_profile import PSA_AGREEMENT_TEMPLATE_ID as TEMPLATE_AGREEMENT_ID
from app.org_profile import PSA_AGREEMENT_TYPE_ID as AGREEMENT_TYPE_ID
from app.services import vendor_client
from app.services.client_naming import validate_client_code, validate_standard_name
from app.services.commands import Command, CommandError, Plan, register

PSA_STATUS_ACTIVE, PSA_TYPE_CLIENT = 1, 1
COMM_EMAIL, COMM_DIRECT = 1, 2

LINES = PSA_PRODUCT_LINES
# Agreement settings copied from the reference agreement (references sent as {id} only).
_TEMPLATE_FIELDS = (
    "location", "department", "billingCycle", "invoicingCycle", "applicationUnits", "applicationLimit",
    "applicationCycle", "applicationUnlimitedFlag", "prorateFlag", "taxable", "billingTerms", "taxCode",
    "workRole", "workType", "coverAgreementTime", "coverAgreementProduct", "coverAgreementExpense",
    "coverSalesTax", "chargeToFirm", "restrictLocationFlag", "restrictDepartmentFlag", "billTime",
    "billExpenses", "billProducts", "billAmount", "sla", "noEndingDateFlag", "carryOverUnused",
)


def first_of_next_month(today: date | None = None) -> str:
    d = today or date.today()
    return (date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)).isoformat()


def _cw_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def catalog_prices() -> dict[str, float]:
    prices: dict[str, float] = {}
    for key, ident in LINES.items():
        r = await vendor_client.get("connectwise_psa", "/procurement/catalog",
                                    {"conditions": f"identifier={_cw_quote(ident)}", "pageSize": 1})
        rows = r["body"] if r["ok"] and isinstance(r["body"], list) else []
        if rows:
            prices[key] = float(rows[0].get("price") or 0)
    return prices


async def _template_agreement() -> tuple[dict[str, Any], dict[str, Any] | None]:
    r = await vendor_client.get("connectwise_psa", f"/finance/agreements/{TEMPLATE_AGREEMENT_ID}")
    if not r["ok"] or not isinstance(r["body"], dict):
        raise CommandError(f"Reference agreement {TEMPLATE_AGREEMENT_ID} not readable — set MSP_AGREEMENT_TEMPLATE_ID")
    a = r["body"]
    fields: dict[str, Any] = {}
    for k in _TEMPLATE_FIELDS:
        v = a.get(k)
        if v is None:
            continue
        fields[k] = {"id": v["id"]} if isinstance(v, dict) and "id" in v else v
    territory = None
    comp = a.get("company") or {}
    if comp.get("id"):
        cr = await vendor_client.get("connectwise_psa", f"/company/companies/{comp['id']}")
        if cr["ok"] and isinstance(cr["body"], dict) and (cr["body"].get("territory") or {}).get("id"):
            territory = {"id": cr["body"]["territory"]["id"]}
    return fields, territory


async def _s1_account_id() -> str:
    r = await vendor_client.get("sentinelone", "/web/api/v2.1/accounts", {"limit": 10})
    rows = (r["body"] or {}).get("data", []) if r["ok"] else []
    if len(rows) != 1:
        raise CommandError("Could not pick the SentinelOne account (expected exactly one)")
    return str(rows[0]["id"])


async def _eset_root_id() -> str:
    c = vendor_client._connector("eset")  # noqa: SLF001
    await c._ensure_auth()  # noqa: SLF001
    async with httpx.AsyncClient(timeout=30) as client:
        cid = await c._get_my_company_id(client)  # noqa: SLF001
    if not cid:
        raise CommandError("Could not read the ESET root company id")
    return cid


def _step(platform: str, method: str, path: str, description: str, body: Any = None,
          params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"kind": "http", "platform": platform, "method": method, "path": path, "params": params or {},
            "body": body, "description": description, "before": None}


async def plan_onboard(p: dict[str, Any]) -> Plan:
    missing = [name for name, value in (("MSP_PSA_AGREEMENT_TYPE_ID", AGREEMENT_TYPE_ID),
                                        ("MSP_AGREEMENT_TEMPLATE_ID", TEMPLATE_AGREEMENT_ID),
                                        ("MSP_ALERT_EMAIL", ALERT_EMAIL)) if not value]
    if missing:
        raise CommandError(f"Onboarding is not configured — set {', '.join(missing)} in .env")
    legal = p["legal_name"].strip()
    std = p["standard_name"].strip()
    code = p["client_code"].strip().upper()
    problems = validate_standard_name(std) + [f"code: {x}" for x in validate_client_code(code)]
    if problems:
        raise CommandError("Fix before proposing: " + "; ".join(problems))
    if p["av_vendor"] not in ("sentinelone", "eset"):
        raise CommandError("av_vendor must be sentinelone or eset")
    for f in ("contact_email",):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", p[f]):
            raise CommandError("Contact email looks invalid")
    start = p.get("start_date") or first_of_next_month()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
        raise CommandError("start_date must be YYYY-MM-DD")

    from sqlalchemy import select

    from app.db.repository import get_session_factory
    from app.db.schema import Client
    async with get_session_factory()() as db:
        if (await db.execute(select(Client).where(Client.client_code == code))).scalar_one_or_none():
            raise CommandError(f"Client code {code} is already used in the registry")

    catalog = await catalog_prices()
    edited = p.get("prices") or {}
    qty = {
        "workstations": int(p.get("workstations") or 0),
        "servers": int(p.get("servers") or 0),
        "network": 1 if p.get("network") else 0,
        "sc_clients": int(p.get("sc_clients") or 0),
        p["av_vendor"]: int(p.get("av_quantity") if p.get("av_quantity") is not None
                            else int(p.get("workstations") or 0) + int(p.get("servers") or 0)),
    }
    template, territory = await _template_agreement()
    steps: list[dict[str, Any]] = []
    warnings: list[str] = []

    def add(step: dict[str, Any]) -> int:
        steps.append(step)
        return len(steps)

    # 1–2. Pre-checks (run again right before anything is written)
    add({"kind": "assert", "platform": "connectwise_psa", "method": "GET", "path": "/company/companies",
         "params": {"conditions": f"identifier={_cw_quote(code)}", "pageSize": 5}, "expect": "empty",
         "description": f"No PSA company already uses Company ID {code}", "before": None})
    if not p.get("allow_similar"):
        add({"kind": "assert", "platform": "connectwise_psa", "method": "GET", "path": "/company/companies",
             "params": {"conditions": f"name={_cw_quote(legal)} and deletedFlag=false", "pageSize": 5},
             "expect": "empty",
             "description": f"No PSA company is already named '{legal}'", "before": None})

    # PSA company (legal name, ID = client code) with Main site
    company: dict[str, Any] = {
        "identifier": code, "name": legal, "status": {"id": PSA_STATUS_ACTIVE}, "types": [{"id": PSA_TYPE_CLIENT}],
        "site": {"name": "Main"}, "addressLine1": p["address1"].strip(), "city": p["city"].strip(),
        "state": p["state"].strip(), "zip": p["zip"].strip(), "phoneNumber": p["phone"].strip(),
    }
    if p.get("address2"):
        company["addressLine2"] = p["address2"].strip()
    if territory:
        company["territory"] = territory
    s_company = add(_step("connectwise_psa", "POST", "/company/companies",
                          f"PSA company '{legal}' (Company ID {code}), Main site, {p['city']}, {p['state']}", company))

    # Main contact (PSA only — ticketing)
    contact = {
        "firstName": p["contact_first"].strip(), "lastName": p["contact_last"].strip(),
        "company": {"id": f"{{{{step:{s_company}.id}}}}"}, "defaultFlag": True,
        "communicationItems": [
            {"type": {"id": COMM_EMAIL}, "value": p["contact_email"].strip(), "defaultFlag": True,
             "communicationType": "Email"},
            {"type": {"id": COMM_DIRECT}, "value": p["contact_phone"].strip(), "defaultFlag": True,
             "communicationType": "Phone"},
        ],
    }
    s_contact = add(_step("connectwise_psa", "POST", "/company/contacts",
                          f"PSA main contact {contact['firstName']} {contact['lastName']} ({p['contact_email']})",
                          contact))
    steps.append({**_step("connectwise_psa", "PATCH", f"/company/companies/{{{{step:{s_company}.id}}}}",
                          "Set the main contact as the company's default contact",
                          [{"op": "replace", "path": "defaultContact", "value": {"id": f"{{{{step:{s_contact}.id}}}}"}}]),
                  "before_ref": {"platform": "connectwise_psa", "path": f"/company/companies/{{{{step:{s_company}.id}}}}"}})

    # Managed-services agreement + additions
    agreement = {
        **template, "name": f"{code} - {PSA_AGREEMENT_LABEL}", "type": {"id": AGREEMENT_TYPE_ID},
        "company": {"id": f"{{{{step:{s_company}.id}}}}"}, "contact": {"id": f"{{{{step:{s_contact}.id}}}}"},
        "startDate": f"{start}T00:00:00Z",
    }
    s_agr = add(_step("connectwise_psa", "POST", "/finance/agreements",
                      f"Agreement '{code} - {PSA_AGREEMENT_LABEL}' (type {AGREEMENT_TYPE_ID}), starts {start}",
                      agreement))
    monthly = 0.0
    for key, n in qty.items():
        if n <= 0:
            continue
        price = float(edited.get(key, catalog.get(key, 0)))
        monthly += price * n
        note = "" if key not in edited or float(edited[key]) == catalog.get(key) else f" (catalog ${catalog.get(key, 0):g})"
        add(_step("connectwise_psa", "POST", f"/finance/agreements/{{{{step:{s_agr}.id}}}}/additions",
                  f"Addition {LINES[key]} × {n} at ${price:g}{note}",
                  {"product": {"identifier": LINES[key]}, "quantity": n, "unitPrice": price,
                   "effectiveDate": f"{start}T00:00:00Z", "billCustomer": "Billable"}))

    # Registry
    add({"kind": "local", "action": "registry_create_client", "description": f"Add {std} ({code}) to the client registry",
         "args": {"client_code": code, "standard_name": std, "legal_name": legal,
                  "psa_company_id": f"{{{{step:{s_company}.id}}}}", "av_vendor": p["av_vendor"]}, "before": None})

    # Antivirus — contact/alerts = support@, never the customer
    av_qty = qty[p["av_vendor"]]
    if p["av_vendor"] == "sentinelone":
        acct = await _s1_account_id()
        s_av = add(_step("sentinelone", "POST", "/web/api/v2.1/sites",
                         f"SentinelOne site '{std}', {av_qty} licenses (alerts inherit the account → {ALERT_EMAIL})",
                         {"data": {"name": std, "accountId": acct, "siteType": "Paid", "usageType": "customer",
                                   "unlimitedExpiration": True, "externalId": code, "description": legal,
                                   "licenses": {"bundles": [{"name": "control", "majorVersion": 1,
                                                             "surfaces": [{"name": "Total Agents",
                                                                           "count": av_qty}]}]}}}))
        add({"kind": "local", "action": "registry_link", "description": "Link the SentinelOne site to the client",
             "args": {"client_code": code, "platform": "sentinelone",
                      "platform_company_id": f"{{{{step:{s_av}.data.id}}}}", "name": std}, "before": None})
    else:
        root = await _eset_root_id()
        add(_step("eset", "POST", "/api/Company/Create/customer",
                  f"ESET company '{std}' with contact email {ALERT_EMAIL}",
                  {"name": std, "email": ALERT_EMAIL, "customIdentifier": code, "parentCompanyId": root,
                   "address": {"streetAddress": p["address1"].strip(), "city": p["city"].strip(),
                               "state": p["state"].strip(), "zipCode": p["zip"].strip()}}))
        add({"kind": "local", "action": "eset_link_by_name", "description": "Find the new ESET company and link it",
             "args": {"client_code": code, "name": std}, "before": None})

    # Asio last: its create API is the least proven, and PSA + AV are already done if it refuses.
    add({"kind": "local", "action": "asio_create_company",
         "description": f"Asio company '{std}' with Main site (then map it to the PSA company in the ConnectWise integration)",
         "args": {"client_code": code, "name": std, "psa_company_id": f"{{{{step:{s_company}.id}}}}"}, "before": None})

    warnings.append(f"Monthly total ${monthly:,.2f} ({PSA_AGREEMENT_LABEL} agreement).")
    warnings.append("Asio: after it runs, map the new Asio company to the PSA company in the ConnectWise "
                    "integration.")
    warnings.append(f"ScreenConnect installer: CP1 = '{std}', CP2 = 'Main', CP5 = the Asio site id.")
    return Plan(f"Onboard new client {std} ({code})", steps, risk="create", warnings=warnings)


async def plan_onboard_finish(p: dict[str, Any]) -> Plan:
    """Create whatever an onboarded client is still missing (AV and/or Asio) — e.g. after a partial run."""
    from sqlalchemy import select

    from app.db.repository import get_session_factory
    from app.db.schema import Client, ClientPlatformLink

    code = p["client_code"].strip().upper()
    async with get_session_factory()() as db:
        client = (await db.execute(select(Client).where(Client.client_code == code))).scalar_one_or_none()
        if client is None:
            raise CommandError(f"No client with code {code} in the registry")
        have = {link.platform for link in (await db.execute(select(ClientPlatformLink).where(
            ClientPlatformLink.client_id == client.id, ClientPlatformLink.status != "rejected"))).scalars()}
    std, legal = client.standard_name, client.legal_name or client.standard_name
    av = p.get("av_vendor") or client.av_vendor
    steps: list[dict[str, Any]] = []
    if av == "sentinelone" and "sentinelone" not in have:
        acct = await _s1_account_id()
        qty = int(p.get("av_quantity") or 1)
        steps.append(_step("sentinelone", "POST", "/web/api/v2.1/sites",
                           f"SentinelOne site '{std}', {qty} licenses (alerts inherit the account → {ALERT_EMAIL})",
                           {"data": {"name": std, "accountId": acct, "siteType": "Paid", "usageType": "customer",
                                     "unlimitedExpiration": True, "externalId": code, "description": legal,
                                     "licenses": {"bundles": [{"name": "control", "majorVersion": 1,
                                                               "surfaces": [{"name": "Total Agents",
                                                                             "count": qty}]}]}}}))
        steps.append({"kind": "local", "action": "registry_link", "description": "Link the SentinelOne site to the client",
                      "args": {"client_code": code, "platform": "sentinelone",
                               "platform_company_id": f"{{{{step:{len(steps)}.data.id}}}}", "name": std},
                      "before": None})
    if p.get("include_asio", True) and "asio" not in have:
        steps.append({"kind": "local", "action": "asio_create_company",
                      "description": f"Asio company '{std}' with Main site (then map it to the PSA company in the ConnectWise integration)",
                      "args": {"client_code": code, "name": std, "psa_company_id": client.psa_company_id},
                      "before": None})
    if not steps:
        raise CommandError(f"{code} already has everything (links: {', '.join(sorted(have))})")
    return Plan(f"Finish onboarding {std} ({code})", steps, risk="create", client_id=client.id)


register(Command(
    "onboard_finish", "Finish onboarding (missing AV / Asio)",
    "For a client already in the registry: create the SentinelOne site and/or Asio company it is still missing.",
    "create",
    {"client_code": {"type": "string", "description": "Registry client code"},
     "av_vendor": {"type": "string", "enum": ["sentinelone", "eset"], "description": "Defaults to the client's AV"},
     "av_quantity": {"type": "integer", "description": "SentinelOne licenses (default 1)"},
     "include_asio": {"type": "boolean", "description": "Create the Asio company if missing (default true)"}},
    ["client_code"], plan_onboard_finish, ["sentinelone", "asio"],
))


_S = lambda d, **kw: {"type": "string", "description": d, **kw}  # noqa: E731
_I = lambda d: {"type": "integer", "description": d}  # noqa: E731

register(Command(
    "onboard_client", "Onboard a new client",
    "Create a new client in PSA (company, Main site, main contact, managed-services agreement + additions), the "
    "registry, and SentinelOne or ESET (alerts to the MSP alert mailbox). Asio follows via the ConnectWise integration.",
    "create",
    {"legal_name": _S("Legal company name (PSA company name, invoices)"),
     "standard_name": _S("Standard name per the naming standard (≤30, no punctuation) used in Asio/S1/ESET/ScreenConnect"),
     "client_code": _S("2–4 letter client code = PSA Company ID"),
     "address1": _S("Street address"), "address2": _S("Suite / line 2"), "city": _S("City"),
     "state": _S("State (2 letters)"), "zip": _S("ZIP"), "phone": _S("Company main phone"),
     "contact_first": _S("Main contact first name"), "contact_last": _S("Main contact last name"),
     "contact_email": _S("Main contact email"), "contact_phone": _S("Main contact direct phone"),
     "workstations": _I("Managed workstations"), "servers": _I("Managed servers"),
     "network": {"type": "boolean", "description": "Managed network"}, "sc_clients": _I("ScreenConnect clients"),
     "av_vendor": _S("Antivirus", enum=["sentinelone", "eset"]),
     "av_quantity": _I("Antivirus seats (default workstations + servers)"),
     "prices": {"type": "object", "description": "Price overrides by line: workstations, servers, network, sc_clients, sentinelone, eset"},
     "start_date": _S("Agreement start YYYY-MM-DD (default first of next month)"),
     "allow_similar": {"type": "boolean", "description": "Proceed even if a PSA company with the same name exists"}},
    ["legal_name", "standard_name", "client_code", "address1", "city", "state", "zip", "phone",
     "contact_first", "contact_last", "contact_email", "contact_phone", "av_vendor"],
    plan_onboard, ["connectwise_psa", "sentinelone", "eset"],
))


def preview_json(plan: Plan) -> str:  # debugging helper
    return json.dumps({"title": plan.title, "steps": plan.steps, "warnings": plan.warnings}, indent=1, default=str)
