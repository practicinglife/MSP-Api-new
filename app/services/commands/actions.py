"""Connector-backed actions for steps that aren't a single REST call.

Each action has an executor (runs only during final-approved execution) and optionally a reader that
returns the current state for the preview and the stale check.
"""

from __future__ import annotations

import json
import os
from typing import Any

from app.services import vendor_client


def _conn(platform: str) -> Any:
    return vendor_client._connector(platform)  # noqa: SLF001


# ── ScreenConnect ─────────────────────────────────────────────────────────


async def sc_read_session(args: dict[str, Any]) -> Any:
    c = _conn("screenconnect")
    details = await c.get_session_details(args["session_id"])
    if not details:
        return None
    session = details.get("Session", details) if isinstance(details, dict) else details
    return {"name": session.get("Name"), "custom_properties": session.get("CustomPropertyValues")}


async def sc_update_custom_properties(args: dict[str, Any]) -> dict[str, Any]:
    c = _conn("screenconnect")
    ok = await c.update_session_custom_properties(args["session_id"], list(args["properties"]))
    return {"ok": bool(ok), "status": 200 if ok else 0, "body": {"updated": bool(ok)}}


# ── SentinelOne ───────────────────────────────────────────────────────────


async def s1_read_agents(args: dict[str, Any]) -> Any:
    r = await vendor_client.get("sentinelone", "/web/api/v2.1/agents",
                                {"ids": ",".join(args["agent_ids"]), "limit": 100})
    if not r["ok"]:
        return None
    return sorted(
        ({"id": a.get("id"), "computerName": a.get("computerName"), "siteName": a.get("siteName")}
         for a in (r["body"] or {}).get("data", [])),
        key=lambda a: str(a["id"]),
    )


async def s1_decommission(args: dict[str, Any]) -> dict[str, Any]:
    c = _conn("sentinelone")
    try:
        out = await c.offboard_device(list(args["agent_ids"]))
        return {"ok": True, "status": 200, "body": out}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 0, "body": {"error": str(exc)}}


# ── Asio ──────────────────────────────────────────────────────────────────


async def asio_remove_endpoint(args: dict[str, Any]) -> dict[str, Any]:
    c = _conn("asio")
    out = await c.remove_agent(args["endpoint_id"], reason=args.get("reason"))
    ok = bool(out) and "error" not in out
    return {"ok": ok, "status": 200 if ok else 0, "body": out}


# ── Local (app-side) actions used by onboarding ───────────────────────────


async def _upsert_pc(db: Any, platform: str, pcid: str, name: str) -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from sqlalchemy import select

    from app.db.schema import PlatformCompany
    from app.services.normalizers import normalize_company_name

    now = datetime.now(UTC)
    pc = (await db.execute(select(PlatformCompany).where(
        PlatformCompany.platform == platform, PlatformCompany.platform_company_id == pcid))).scalar_one_or_none()
    if pc is None:
        db.add(PlatformCompany(id=str(uuid4()), platform=platform, platform_company_id=pcid, name=name,
                               name_norm=normalize_company_name(name), active=True, first_seen_at=now,
                               last_seen_at=now))


async def _link(db: Any, client_id: str, platform: str, pcid: str, name: str, method: str) -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from sqlalchemy import select

    from app.db.schema import ClientPlatformLink

    now = datetime.now(UTC)
    await _upsert_pc(db, platform, pcid, name)
    existing = (await db.execute(select(ClientPlatformLink).where(
        ClientPlatformLink.platform == platform, ClientPlatformLink.platform_company_id == pcid,
        ClientPlatformLink.client_id == client_id))).scalar_one_or_none()
    if existing:
        existing.status, existing.updated_at = "confirmed", now
    else:
        db.add(ClientPlatformLink(id=str(uuid4()), client_id=client_id, platform=platform,
                                  platform_company_id=pcid, status="confirmed", link_method=method,
                                  confidence=1.0, evidence={"platform_name": name}, created_at=now, updated_at=now))


async def registry_create_client(args: dict[str, Any]) -> dict[str, Any]:
    """Create (or reuse by code) the registry client and link its new PSA company."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from sqlalchemy import select

    from app.db.repository import get_session_factory
    from app.db.schema import Client

    now = datetime.now(UTC)
    async with get_session_factory()() as db:
        c = (await db.execute(select(Client).where(Client.client_code == args["client_code"]))).scalar_one_or_none()
        if c is None:
            c = Client(id=str(uuid4()), client_code=args["client_code"], standard_name=args["standard_name"],
                       legal_name=args.get("legal_name"), psa_company_id=str(args["psa_company_id"]),
                       av_vendor=args.get("av_vendor"), status="active", naming_status="approved",
                       notes="Created by onboarding form", created_at=now, updated_at=now)
            db.add(c)
            await db.flush()
        await _link(db, c.id, "connectwise_psa", str(args["psa_company_id"]), args.get("legal_name") or
                    args["standard_name"], "onboarding")
        await db.commit()
    return {"ok": True, "status": 200, "body": {"client_id": c.id, "client_code": c.client_code}}


async def registry_link(args: dict[str, Any]) -> dict[str, Any]:
    """Link a newly created platform company (by id) to the client with this code."""
    from sqlalchemy import select

    from app.db.repository import get_session_factory
    from app.db.schema import Client

    async with get_session_factory()() as db:
        c = (await db.execute(select(Client).where(Client.client_code == args["client_code"]))).scalar_one_or_none()
        if c is None:
            return {"ok": False, "status": 0, "body": {"error": f"client {args['client_code']} not in registry"}}
        await _link(db, c.id, args["platform"], str(args["platform_company_id"]), args["name"], "onboarding")
        await db.commit()
    return {"ok": True, "status": 200, "body": {"linked": args["platform"], "id": str(args["platform_company_id"])}}


async def eset_link_by_name(args: dict[str, Any]) -> dict[str, Any]:
    """ESET's create returns no id — find the new company by exact name and link it."""
    c = _conn("eset")
    try:
        companies = await c.get_companies()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 0, "body": {"error": f"ESET lookup failed: {exc}"}}
    match = next((r for r in companies if str((r.raw or {}).get("name") or (r.raw or {}).get("Name") or "")
                  .strip().casefold() == args["name"].strip().casefold()), None)
    if not match:
        return {"ok": False, "status": 0, "body": {"error": f"ESET company '{args['name']}' not found after create"}}
    return await registry_link({"client_code": args["client_code"], "platform": "eset",
                                "platform_company_id": match.platform_id, "name": args["name"]})


async def psa_delete_company_contacts(args: dict[str, Any]) -> dict[str, Any]:
    """Delete whatever contacts are left on a PSA company (some may already be gone with the company)."""
    cid = int(args["psa_company_id"])
    r = await vendor_client.get("connectwise_psa", "/company/contacts",
                                {"conditions": f"company/id={cid}", "pageSize": 100})
    rows = r["body"] if r["ok"] and isinstance(r["body"], list) else []
    deleted, kept, errors = [], [], []
    for row in rows:
        d = await vendor_client.request("connectwise_psa", "DELETE", f"/company/contacts/{row['id']}")
        if d["ok"] or d["status"] == 404:
            deleted.append(row["id"])
        elif d["status"] == 409 and "default contact" in json.dumps(d["body"]).lower():
            # PSA never lets the default contact go; it stays hidden with the deleted company.
            kept.append(row["id"])
        else:
            errors.append({"id": row["id"], "status": d["status"], "body": d["body"]})
    return {"ok": not errors, "status": 200 if not errors else 0,
            "body": {"deleted_contacts": deleted,
                     **({"kept_default_contact": kept} if kept else {}), **({"errors": errors} if errors else {})}}


async def eset_suspend_then_delete(args: dict[str, Any]) -> dict[str, Any]:
    """ESET deletes only suspended companies and suspension takes 1–3 minutes: suspend, then retry the delete."""
    import asyncio

    cid = args["company_id"]
    s = await vendor_client.request("eset", "POST", "/api/Company/Suspend", None, {"companyId": cid})
    if not s["ok"] and "suspend" not in json.dumps(s["body"]).lower():
        return {"ok": False, "status": s["status"], "body": {"step": "suspend", "response": s["body"]}}
    waited, last = 0, None
    while waited <= int(args.get("max_wait_seconds", 360)):
        await asyncio.sleep(20)
        waited += 20
        last = await vendor_client.request("eset", "POST", "/api/Company/Delete", None, {"companyId": cid})
        if last["ok"]:
            return {"ok": True, "status": last["status"], "body": {"suspended": True, "deleted": cid,
                                                                   "waited_seconds": waited}}
    return {"ok": False, "status": (last or {}).get("status", 0),
            "body": {"error": f"ESET company still not deletable after {waited}s — it may still be suspending; "
                              "delete it in ESET", "response": (last or {}).get("body")}}


async def asio_create_company(args: dict[str, Any]) -> dict[str, Any]:
    """Create the Asio company (so the ConnectWise integration can be mapped to it) and link it."""
    # Address/phone from the PSA company (system of record), shaped like existing Asio companies.
    tz = os.environ.get("MSP_DEFAULT_TIMEZONE", "(UTC) Coordinated Universal Time")
    site: dict[str, Any] = {"name": "Main", "friendlyName": args["name"], "physicalAddressFlag": True,
                            "timeZone": tz}
    if args.get("psa_company_id"):
        r = await vendor_client.get("connectwise_psa", f"/company/companies/{args['psa_company_id']}")
        p = r["body"] if r["ok"] and isinstance(r["body"], dict) else {}
        if p.get("addressLine1"):
            site["primaryAddress"] = {k: v for k, v in {
                "addressLine1": p.get("addressLine1"), "addressLine2": p.get("addressLine2"),
                "locality": p.get("city"), "administrativeArea": p.get("state"), "postCode": p.get("zip"),
                "countryCode": "US", "timeZone": tz, "primaryFlag": True}.items() if v}
        if p.get("phoneNumber"):
            site["primaryPhoneNumber"] = {"nationalNumber": p["phoneNumber"], "countryCode": "US",
                                          "primaryFlag": True, "activeFlag": True}
    body = {"name": args["name"], "friendlyName": args["name"], "primarySite": site}
    r = await vendor_client.request("asio", "POST", "/platform/v1/company/companies", None, body)
    if r["status"] == 403:
        # Permissions are baked into the token — after a portal change, retry once with a fresh token.
        c = _conn("asio")
        c._access_token, c._token_expires_at = "", 0  # noqa: SLF001
        r = await vendor_client.request("asio", "POST", "/platform/v1/company/companies", None, body)
    phone_note = None
    if r["status"] == 400 and "phone" in json.dumps(r["body"]).lower() and site.get("primaryPhoneNumber"):
        # Asio validates numbers strictly (e.g. 555-01xx test numbers) — don't let the phone block the company.
        phone_note = f"Asio rejected phone {site['primaryPhoneNumber']['nationalNumber']} — created without it; add it in Asio"
        site.pop("primaryPhoneNumber")
        r = await vendor_client.request("asio", "POST", "/platform/v1/company/companies", None, body)
    if not r["ok"] or not isinstance(r["body"], dict) or not r["body"].get("id"):
        return {"ok": False, "status": r["status"], "body": {"request": body, "response": r["body"]}}
    out = {"id": str(r["body"]["id"]), "name": r["body"].get("name"), "raw": r["body"]}
    linked = await registry_link({"client_code": args["client_code"], "platform": "asio",
                                  "platform_company_id": out["id"], "name": out.get("name") or args["name"]})
    return {"ok": linked["ok"], "status": 200, "body": {"asio_company_id": out["id"], "raw": out.get("raw"),
                                                        "link": linked["body"],
                                                        **({"note": phone_note} if phone_note else {})}}


async def registry_delete_client(args: dict[str, Any]) -> dict[str, Any]:
    """Remove a client and its platform links from the registry."""
    from sqlalchemy import delete, select

    from app.db.repository import get_session_factory
    from app.db.schema import Client, ClientPlatformLink

    async with get_session_factory()() as db:
        c = (await db.execute(select(Client).where(Client.client_code == args["client_code"]))).scalar_one_or_none()
        if c is None:
            return {"ok": True, "status": 200, "body": {"removed": None, "note": "not in registry"}}
        links = (await db.execute(delete(ClientPlatformLink).where(ClientPlatformLink.client_id == c.id))).rowcount
        await db.delete(c)
        await db.commit()
    return {"ok": True, "status": 200, "body": {"removed": args["client_code"], "links_removed": links}}


async def _review_items(args: dict[str, Any], status: str, note: str) -> None:
    """Create/advance AvReviewItem rows for the machines a step acted on (ties them to the approved change)."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from sqlalchemy import select

    from app.db.repository import get_session_factory
    from app.db.schema import AvReviewItem

    now = datetime.now(UTC)
    async with get_session_factory()() as db:
        for eid, host in zip(args["endpoint_ids"], args.get("hostnames") or [""] * len(args["endpoint_ids"])):
            it = (await db.execute(select(AvReviewItem).where(
                AvReviewItem.endpoint_id == eid,
                AvReviewItem.status.in_(("flagged", "deploy_sent", "not_fixed"))))).scalar_one_or_none()
            if it is None:
                it = AvReviewItem(id=str(uuid4()), endpoint_id=eid, client_id=args.get("client_id"), hostname=host,
                                  expected_av=args.get("expected_av", ""), flagged_at=now)
                db.add(it)
            it.status, it.change_id, it.note, it.checks = status, args.get("_change_id"), note, 0
        await db.commit()


async def asio_set_av_flag(args: dict[str, Any]) -> dict[str, Any]:
    """Set the 'AV Status' custom field on machines → they appear in the Asio review group."""
    from app.services.av_coverage import FLAG_FIELD, FLAG_VALUE, flag_attribute_id

    attr = await flag_attribute_id()
    if not attr:
        return {"ok": False, "status": 0, "body": {
            "error": f"Asio has no custom field named '{FLAG_FIELD}' — create it (Endpoint level) in the Asio "
                     "console, plus a dynamic group filtering on it, then propose again."}}
    c = _conn("asio")
    done, failed = [], []
    for eid in args["endpoint_ids"]:
        r = await c.set_endpoint_custom_field(eid, attr, args.get("value", FLAG_VALUE))
        (done if r["ok"] else failed).append({"endpoint_id": eid, **({} if r["ok"] else {"status": r["status"],
                                                                                         "body": r["body"]})})
    if done:
        await _review_items({**args, "endpoint_ids": [d["endpoint_id"] for d in done],
                             "hostnames": [h for e, h in zip(args["endpoint_ids"], args.get("hostnames") or [])
                                           if e in {d["endpoint_id"] for d in done}]},
                            "flagged", "Flagged for review in Asio")
    return {"ok": not failed, "status": 200 if not failed else 0,
            "body": {"flagged": len(done), **({"failed": failed} if failed else {})}}


async def asio_run_av_script(args: dict[str, Any]) -> dict[str, Any]:
    """Run the AV install script (by name) through Asio on the client's machines."""
    c = _conn("asio")
    scripts = await c.list_scripts(company_id=args.get("company_id")) or []
    name = args["script_name"].strip().casefold()
    script = next((s for s in scripts if str(s.get("name", "")).strip().casefold() == name), None)
    if not script:
        return {"ok": False, "status": 0, "body": {
            "error": f"No Asio script named '{args['script_name']}'", "available": [s.get("name") for s in scripts][:40]}}
    out = await c.run_script(str(script["id"]), list(args["endpoint_ids"]), parameters=args.get("parameters") or {})
    ok = bool(out) and not out.get("error")
    if ok:
        await _review_items(args, "deploy_sent", f"Install sent via Asio script '{script.get('name')}'")
    return {"ok": ok, "status": 200 if ok else 0, "body": {"script": script.get("name"), "script_id": script["id"],
                                                         "machines": len(args["endpoint_ids"]), "asio": out}}


async def library_read(args: dict[str, Any]) -> Any:
    from app.services.commands.library import target_path

    path = target_path(args["kind"], args["name"])
    return path.read_text(encoding="utf-8") if path.exists() else None


async def library_write(args: dict[str, Any]) -> dict[str, Any]:
    """Write (or delete) a library file and make it live. Content was validated when the change was planned."""
    import shutil

    from app.services.commands import REGISTRY
    from app.services.commands.library import target_path

    kind, name, content = args["kind"], args["name"], args.get("content")
    path = target_path(kind, name)
    if content is None:
        if kind == "skill":
            shutil.rmtree(path.parent, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        if kind == "playbook":
            REGISTRY.pop(f"playbook:{name}", None)
        return {"ok": True, "status": 200, "body": {"removed": f"{kind}:{name}"}}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    if kind == "playbook":
        from app.services.commands.playbooks import load_playbooks
        load_playbooks()  # re-registers every playbook, including this one
    from app.services.commands.library import APP_DIR
    return {"ok": True, "status": 200, "body": {"saved": f"{kind}:{name}", "path": str(path.relative_to(APP_DIR))}}


async def connection_call(args: dict[str, Any]) -> dict[str, Any]:
    """An approved write to an imported connection (write mode 'approval'). The change engine already checked
    that the connection isn't read-only (the step carries ``platform`` = the connection slug)."""
    from app.auth.principal import SYSTEM
    from app.connections.base import CallError, UpstreamError
    from app.connections.service import call_operation

    try:
        out = await call_operation(args["connection"], args["operation"], args.get("args") or {},
                                   approved=True, principal=SYSTEM)
        return {"ok": True, "status": 200, "body": out.get("result")}
    except UpstreamError as exc:
        return {"ok": False, "status": exc.status, "body": {"error": str(exc), "detail": exc.detail}}
    except CallError as exc:
        return {"ok": False, "status": exc.status, "body": {"error": str(exc)}}


LOCAL = {
    "registry_create_client": registry_create_client,
    "registry_link": registry_link,
    "eset_link_by_name": eset_link_by_name,
    "psa_delete_company_contacts": psa_delete_company_contacts,
    "registry_delete_client": registry_delete_client,
    "eset_suspend_then_delete": eset_suspend_then_delete,
    "asio_create_company": asio_create_company,
    "library_write": library_write,
}

ACTIONS = {
    "sc_update_custom_properties": sc_update_custom_properties,
    "s1_decommission": s1_decommission,
    "asio_remove_endpoint": asio_remove_endpoint,
    "asio_set_av_flag": asio_set_av_flag,
    "asio_run_av_script": asio_run_av_script,
    "connection_call": connection_call,
}
READERS = {
    "sc_read_session": sc_read_session,
    "s1_read_agents": s1_read_agents,
    "library_read": library_read,
}
