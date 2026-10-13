"""Antivirus coverage: which managed machines are missing their antivirus, and the reverse (AV but no Asio agent).

Evidence per machine (keyed by registry client + host key):
* **Asio** — the antivirus products Asio itself detects on the endpoint (``antiViruses``), swept into
  ``asio_endpoint_av`` (a row with no vendors = Asio checked and found nothing).
* **SentinelOne** — the machine has a SentinelOne agent (``platform_devices`` platform ``sentinelone``).
* **ESET** — ESET Connect lists the machine with ESET components (``device_av_status`` source ``eset_connect``).

Expected AV = the registry client's ``av_vendor``. A machine in Asio is:
``ok`` · ``no_antivirus`` (nothing, or Defender only) · ``wrong_antivirus`` (the other vendor) ·
``stale`` (Asio record untouched > 30 days — listed, never flagged).

Fixing goes through change requests (``app/services/commands/av.py``): flag in Asio (custom field → dynamic group
"No Antivirus – Need Immediate Review"), redeploy via an Asio script, then the next sweep verifies and unflags.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.schema import (
    AsioEndpointAv,
    AvReviewItem,
    Client,
    ClientPlatformLink,
    DeviceAvStatus,
    PlatformCompany,
    PlatformDevice,
)
from app.services.normalizers import hostname_match_key, normalize_hostname

log = structlog.get_logger(__name__)

AV_VENDORS = ("sentinelone", "eset")
FLAG_FIELD = os.environ.get("MSP_AV_FLAG_FIELD", "AV Status")
FLAG_VALUE = os.environ.get("MSP_AV_FLAG_VALUE", "No Antivirus - Needs Review")
S1_SCRIPT = os.environ.get("MSP_S1_INSTALL_SCRIPT", "sentinelone installer")
ESET_SCRIPT = os.environ.get("MSP_ESET_INSTALL_SCRIPT", "eset web installer")
STALE_DAYS = int(os.environ.get("MSP_AV_STALE_DAYS", "30"))


def host_key(hostname: str) -> str:
    return hostname_match_key(normalize_hostname(hostname or ""))[:15]


def classify(product: dict[str, Any] | str) -> str:
    """'sentinelone' | 'eset' | 'defender' | 'other' from an Asio antivirus entry."""
    if isinstance(product, dict):
        text = " ".join(str(product.get(k) or "") for k in ("name", "applicationName"))
    else:
        text = str(product)
    t = text.lower()
    if "sentinel" in t:
        return "sentinelone"
    if "eset" in t:
        return "eset"
    if "defender" in t or "windows security" in t:
        return "defender"
    return "other" if t.strip() else ""


EXCLUDED_ROLES = {"hyperv": "Hyper-V host", "sql": "SQL server", "dc": "Domain controller"}
_ROLE_TOOLS = ("tools", "powershell", "management", "rsat", "snap-in", "module")
_SQL_NOT_ENGINE = ("localdb", "management studio", "native client", "odbc", "compact", "clr types", "report",
                   "setup", "browser", "vss writer", "policies", "shared management", "t-sql", "dac", "data-tier",
                   "language service", "smo", "client", "command line utilities", "ole db", "redist", "uninstall")


def is_server_os(endpoint_type: str, os_name: str) -> bool:
    return (endpoint_type or "").strip().lower() == "server" or "server" in (os_name or "").lower()


def detect_roles(roles_features: list[str], is_server: bool) -> list[dict[str, str]]:
    """Hyper-V host / domain controller from Asio's roles & features (servers only; admin tools don't count)."""
    if not is_server:
        return []
    found: list[dict[str, str]] = []
    for r in roles_features or []:
        t = str(r).strip().lower()
        if any(x in t for x in _ROLE_TOOLS):
            continue
        if t in ("hyper-v", "microsoft-hyper-v", "microsoft-hyper-v-all") or t.startswith("hyper-v ("):
            found.append({"role": "hyperv", "evidence": str(r)})
        elif "active directory domain services" in t or t == "ad-domain-services":
            found.append({"role": "dc", "evidence": str(r)})
    seen: set[str] = set()
    return [f for f in found if not (f["role"] in seen or seen.add(f["role"]))]


def detect_sql(app_names: list[str]) -> dict[str, str] | None:
    """SQL Server database engine among a server's installed applications (Express is labelled)."""
    engine = [n for n in app_names if "sql server" in n.lower()
              and not any(x in n.lower() for x in _SQL_NOT_ENGINE)
              and (re.search(r"sql server (19|20)\d\d", n.lower()) or "database engine" in n.lower())]
    if not engine:
        return None
    express = any("express" in n.lower() for n in app_names if "sql server" in n.lower())
    return {"role": "sql", "evidence": engine[0] + (" — Express edition" if express else "")}


def effective_roles(detected: list[dict[str, str]] | None, override: str | None) -> list[str]:
    """Roles after a manual override ('none' clears detection; a role replaces it)."""
    if override == "none":
        return []
    if override in EXCLUDED_ROLES:
        return [override]
    return [r["role"] for r in (detected or []) if r.get("role") in EXCLUDED_ROLES]


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


# ── Sweep ──────────────────────────────────────────────────────────────────


async def sweep(session: AsyncSession, *, refresh_eset_connect: bool = True) -> dict[str, Any]:
    """Ask Asio which antivirus it sees on every known Asio endpoint; refresh ESET Connect evidence; verify fixes."""
    from app.services import vendor_client
    from app.services.client_registry import apply_registry_to_devices

    await apply_registry_to_devices(session)  # make sure every device carries its registry client
    asio_rows = (await session.execute(select(
        PlatformDevice.endpoint_id, PlatformDevice.platform_id, PlatformDevice.client_id,
        PlatformDevice.company_platform_id, PlatformDevice.hostname).where(PlatformDevice.platform == "asio"))).all()
    by_ep = {str(r.endpoint_id or r.platform_id): r for r in asio_rows if (r.endpoint_id or r.platform_id)}
    asio = vendor_client._connector("asio")  # noqa: SLF001
    endpoints = await asio.get_endpoints_antivirus(list(by_ep))
    heartbeat = await _heartbeats(asio, [e["endpoint_id"] for e in endpoints])

    # Server roles that shouldn't run antivirus: Hyper-V / DC from roles & features, SQL from installed apps
    servers = [e["endpoint_id"] for e in endpoints if is_server_os(e.get("endpoint_type", ""), e.get("os_product", ""))]
    sql_apps: dict[str, list[str]] = defaultdict(list)
    if servers:
        try:
            for entry in await asio.get_endpoint_applications(servers, app_name_filter="SQL Server"):
                sql_apps[str(entry.get("endpointID", ""))] += [str(a.get("name") or "") for a in entry.get("applications") or []]
        except Exception as exc:  # noqa: BLE001 — roles are best-effort; overrides can fill gaps
            log.warning("av.sql_detection_failed", error=str(exc))

    now = datetime.now(UTC)
    await session.execute(delete(AsioEndpointAv))
    await session.execute(delete(DeviceAvStatus).where(DeviceAvStatus.detection_source == "asio_detected"))
    no_av = 0
    written: set[tuple[str, str]] = set()
    for ep in endpoints:
        src = by_ep.get(ep["endpoint_id"])
        vendors = sorted({v for v in (classify(a) for a in ep["antiviruses"]) if v})
        no_av += not vendors
        hostname = ep["hostname"] or (src.hostname if src else "")
        server = is_server_os(ep.get("endpoint_type", ""), ep.get("os_product", ""))
        roles = detect_roles(ep.get("roles") or [], server)
        sql = detect_sql(sql_apps.get(ep["endpoint_id"], [])) if server else None
        if sql:
            roles.append(sql)
        session.add(AsioEndpointAv(
            os_name=str(ep.get("os_product") or "")[:128], is_server=server, roles=roles,
            endpoint_id=ep["endpoint_id"], client_id=src.client_id if src else None,
            company_platform_id=ep["company_id"] or (src.company_platform_id if src else ""),
            hostname=hostname, host_key=host_key(hostname), products=ep["antiviruses"], vendors=vendors,
            modified_at=str(ep["modified_at"])[:40], agent_installed_at=str(ep["agent_installed_at"])[:40],
            last_contact=heartbeat.get(ep["endpoint_id"], ("", None))[0][:40],
            online=heartbeat.get(ep["endpoint_id"], ("", None))[1], swept_at=now))
        # Also feed the breakdown's per-device AV evidence (it never had Asio's view, nor any for ESET clients).
        # One row per (machine, vendor): prefer the protection product over e.g. "ESET Management Agent".
        for a in sorted(ep["antiviruses"], key=lambda x: "agent" in str(x.get("name", "")).lower()
                        and "sentinel" not in str(x.get("name", "")).lower()):
            v = classify(a)
            key = (normalize_hostname(hostname), v)
            if v in ("sentinelone", "eset", "defender") and key not in written:
                written.add(key)
                session.add(DeviceAvStatus(
                    id=str(uuid4()), hostname=hostname, hostname_norm=normalize_hostname(hostname),
                    endpoint_id=ep["endpoint_id"], company_canonical="", av_vendor=v,
                    av_product=str(a.get("name") or a.get("applicationName") or "")[:256],
                    av_version=str(a.get("version") or "")[:128], av_status="installed",
                    detection_source="asio_detected", detected_at=now, created_at=now, updated_at=now))
    await session.commit()

    eset_result: Any = None
    if refresh_eset_connect:
        try:
            from app.api.devices import scan_eset_connect_devices
            eset_result = await scan_eset_connect_devices(session=session)
            eset_result = {k: eset_result.get(k) for k in ("status", "stored", "devices", "detail") if k in eset_result}
        except Exception as exc:  # noqa: BLE001
            eset_result = {"error": str(exc)}

    verified = await verify_review_items(session)
    summary = {"asio_endpoints_known": len(by_ep), "asio_endpoints_returned": len(endpoints),
               "no_antivirus_reported": no_av, "heartbeat_records": len(heartbeat),
               **({"heartbeat_errors": _HEARTBEAT_ERRORS[:3]} if _HEARTBEAT_ERRORS else {}),"eset_connect": eset_result, "verification": verified,
               "swept_at": now.isoformat()}
    log.info("av.sweep", **{k: v for k, v in summary.items() if k != "verification"})
    return summary


_HEARTBEAT_ERRORS: list[str] = []


async def _heartbeats(asio: Any, endpoint_ids: list[str]) -> dict[str, tuple[str, bool | None]]:
    """endpoint id → (last contact UTC, online) from Asio's heartbeat API.

    The ids go in the query string, so 100 per call (500 × 37-char ids exceeds Asio's URL limit → HTTP 414).
    """
    _HEARTBEAT_ERRORS.clear()
    out: dict[str, tuple[str, bool | None]] = {}
    for i in range(0, len(endpoint_ids), 100):
        data = await asio.get_endpoint_heartbeat(endpoint_ids[i: i + 100])
        if not isinstance(data, dict) or data.get("error"):
            log.warning("av.heartbeat_failed", error=(data or {}).get("error") if isinstance(data, dict) else data)
            _HEARTBEAT_ERRORS.append(str((data or {}).get("error") if isinstance(data, dict) else data)[:300])
            continue
        for rec in data.get("successfulRecords") or []:
            for ep in rec.get("endpoints") or []:
                eid = str(ep.get("EndpointID") or ep.get("endpointID") or "")
                if eid:
                    out[eid] = (str(ep.get("DcDateTimeUTC") or ""), ep.get("Availability"))
    return out


# ── Gap detection ──────────────────────────────────────────────────────────


async def _context(session: AsyncSession) -> dict[str, Any]:
    clients = {c.id: c for c in (await session.execute(select(Client))).scalars()}
    s1_hosts: dict[str | None, set[str]] = defaultdict(set)
    asio_hosts: dict[str | None, set[str]] = defaultdict(set)
    s1_devices: list[Any] = []
    for d in (await session.execute(select(PlatformDevice.platform, PlatformDevice.client_id, PlatformDevice.hostname,
                                           PlatformDevice.last_seen_at, PlatformDevice.company_raw)
                                    .where(PlatformDevice.platform.in_(("sentinelone", "asio"))))).all():
        hk = host_key(d.hostname)
        if not hk:
            continue
        (s1_hosts if d.platform == "sentinelone" else asio_hosts)[d.client_id].add(hk)
        if d.platform == "sentinelone":
            s1_devices.append(d)
    eset_rows = (await session.execute(select(DeviceAvStatus.hostname, DeviceAvStatus.company_canonical,
                                              DeviceAvStatus.av_product, DeviceAvStatus.detected_at)
                                       .where(DeviceAvStatus.detection_source == "eset_connect"))).all()
    eset_hosts = {host_key(r.hostname) for r in eset_rows if r.hostname}
    swept = list((await session.execute(select(AsioEndpointAv))).scalars())
    return {"clients": clients, "s1_hosts": s1_hosts, "asio_hosts": asio_hosts, "eset_hosts": eset_hosts,
            "eset_rows": eset_rows, "s1_devices": s1_devices, "swept": swept}


def _status(expected: str, found: set[str], stale: bool) -> str:
    if expected in found:
        return "ok"
    if stale:
        return "stale"
    if found & (set(AV_VENDORS) - {expected}):
        return "wrong_antivirus"
    return "no_antivirus"


async def compute_gaps(session: AsyncSession, client_id: str | None = None,
                       ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """Machines in Asio that don't have their client's antivirus, grouped by client."""
    ctx = ctx or await _context(session)
    clients: dict[str, Client] = ctx["clients"]
    from app.db.schema import MachineRoleOverride
    overrides = {o.endpoint_id: o.role for o in (await session.execute(select(MachineRoleOverride))).scalars()}
    open_items = {i.endpoint_id: i for i in (await session.execute(
        select(AvReviewItem).where(AvReviewItem.status.in_(("flagged", "deploy_sent", "not_fixed"))))).scalars()}
    stale_before = datetime.now(UTC) - timedelta(days=STALE_DAYS)
    per_client: dict[str, dict[str, Any]] = {}
    fix_client_av: dict[str, dict[str, Any]] = {}
    totals: dict[str, int] = defaultdict(int)
    for row in ctx["swept"]:
        if client_id and row.client_id != client_id:
            continue
        c = clients.get(row.client_id) if row.client_id else None
        if c is None:
            totals["unlinked_company"] += 1
            continue
        roles = effective_roles(row.roles, overrides.get(row.endpoint_id))
        if roles:
            # Hyper-V hosts, SQL servers and DCs should run NO antivirus — present = flag to remove
            found_av = set(row.vendors or []) & set(AV_VENDORS)
            if row.host_key in ctx["s1_hosts"].get(c.id, set()):
                found_av.add("sentinelone")
            if row.host_key in ctx["eset_hosts"]:
                found_av.add("eset")
            if not found_av:
                totals["excluded_server_ok"] += 1
                continue
            totals["av_on_excluded_server"] += 1
            entry = per_client.setdefault(c.id, {
                "client_id": c.id, "client_code": c.client_code, "client": c.standard_name,
                "av_vendor": c.av_vendor, "machines": []})
            entry["machines"].append({
                "endpoint_id": row.endpoint_id, "hostname": row.hostname, "status": "av_on_excluded_server",
                "found": sorted(found_av), "defender_only": False, "roles": roles,
                "role_names": [EXCLUDED_ROLES[r] for r in roles],
                "asio_products": [p.get("name") or p.get("applicationName") for p in (row.products or [])],
                "last_contact": row.last_contact, "online": row.online, "agent_installed": row.agent_installed_at,
                "review": None})
            continue
        expected = c.av_vendor
        if expected not in AV_VENDORS:
            fix_client_av.setdefault(c.id, {"client_id": c.id, "client_code": c.client_code,
                                            "client": c.standard_name, "av_vendor": expected, "machines": 0})
            fix_client_av[c.id]["machines"] += 1
            totals["client_av_unknown"] += 1
            continue
        found = set(row.vendors or [])
        if row.host_key in ctx["s1_hosts"].get(c.id, set()):
            found.add("sentinelone")
        if row.host_key in ctx["eset_hosts"]:
            found.add("eset")
        last_contact = _parse_dt(row.last_contact)
        status = _status(expected, found, bool(last_contact and last_contact < stale_before))
        totals[status] += 1
        if status == "ok":
            continue
        entry = per_client.setdefault(c.id, {
            "client_id": c.id, "client_code": c.client_code, "client": c.standard_name, "av_vendor": expected,
            "machines": []})
        item = open_items.get(row.endpoint_id)
        entry["machines"].append({
            "endpoint_id": row.endpoint_id, "hostname": row.hostname, "status": status,
            "found": sorted(found) or ["nothing"],
            "defender_only": found == {"defender"},
            "asio_products": [p.get("name") or p.get("applicationName") for p in (row.products or [])],
            "last_contact": row.last_contact, "online": row.online, "agent_installed": row.agent_installed_at,
            "review": {"status": item.status, "change_id": item.change_id, "checks": item.checks} if item else None,
        })
    rows = sorted(per_client.values(), key=lambda e: (-len(e["machines"]), e["client"] or ""))
    for e in rows:
        e["machines"].sort(key=lambda m: (m["status"] != "no_antivirus", m["hostname"]))
        e["counts"] = {s: sum(1 for m in e["machines"] if m["status"] == s)
                       for s in ("no_antivirus", "wrong_antivirus", "stale", "av_on_excluded_server")}
    swept_at = max((r.swept_at for r in ctx["swept"]), default=None)
    return {"swept_at": swept_at.isoformat() if swept_at else None, "checked": len(ctx["swept"]),
            "totals": dict(totals), "clients": rows, "fix_client_av": list(fix_client_av.values())}


async def missing_asio(session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """Machines that have SentinelOne or ESET but no Asio agent (device name + company)."""
    ctx = ctx or await _context(session)
    clients: dict[str, Client] = ctx["clients"]
    all_asio = set().union(*ctx["asio_hosts"].values()) if ctx["asio_hosts"] else set()
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for d in ctx["s1_devices"]:
        hk = host_key(d.hostname)
        if hk in all_asio or (hk, "sentinelone") in seen:
            continue
        seen.add((hk, "sentinelone"))
        c = clients.get(d.client_id) if d.client_id else None
        out.append({"hostname": d.hostname, "antivirus": "SentinelOne",
                    "client_code": c.client_code if c else None,
                    "company": c.standard_name if c else (d.company_raw or "(unlinked)"),
                    "last_seen": d.last_seen_at.isoformat() if d.last_seen_at else None})
    for r in ctx["eset_rows"]:
        hk = host_key(r.hostname)
        if not hk or hk in all_asio or (hk, "eset") in seen:
            continue
        seen.add((hk, "eset"))
        out.append({"hostname": r.hostname, "antivirus": f"ESET ({r.av_product})" if r.av_product else "ESET",
                    "client_code": None, "company": r.company_canonical or "(unknown)",
                    "last_seen": r.detected_at.isoformat() if r.detected_at else None})
    # Not seen for STALE_DAYS → most likely a retired machine still listed in SentinelOne/ESET
    cutoff = datetime.now(UTC) - timedelta(days=STALE_DAYS)
    for m in out:
        seen = _parse_dt(m["last_seen"] or "")
        m["old"] = bool(seen and seen < cutoff)
    out.sort(key=lambda x: (x["old"], (x["company"] or "").lower(), x["hostname"]))
    return {"count": len(out), "active": sum(1 for m in out if not m["old"]),
            "old": sum(1 for m in out if m["old"]), "machines": out}


# ── Deployment prep ────────────────────────────────────────────────────────


async def deployment_for_client(session: AsyncSession, client: Client) -> dict[str, Any]:
    """What a redeploy would run for this client: script name + parameters (S1 site token / ESET installer)."""
    from app.services import vendor_client

    links = list((await session.execute(select(ClientPlatformLink).where(
        ClientPlatformLink.client_id == client.id, ClientPlatformLink.status == "confirmed"))).scalars())
    asio_link = next((ln for ln in links if ln.platform == "asio"), None)
    out: dict[str, Any] = {"av_vendor": client.av_vendor,
                           "asio_company_id": asio_link.platform_company_id if asio_link else None, "problems": []}
    if not asio_link:
        out["problems"].append("No Asio company linked to this client — link it in the client registry first.")
    if client.av_vendor == "sentinelone":
        site = next((ln for ln in links if ln.platform == "sentinelone"), None)
        out["script"] = S1_SCRIPT
        installer = os.environ.get("MSP_S1_INSTALLER_URL", "")
        token = ""
        if site:
            r = await vendor_client.get("sentinelone", "/web/api/v2.1/sites", {"siteIds": site.platform_company_id})
            sites = ((r["body"] or {}).get("data") or {}).get("sites") or [] if r["ok"] else []
            token = (sites[0].get("registrationToken") or "") if sites else ""
            out["site"] = {"id": site.platform_company_id, "name": sites[0].get("name") if sites else None}
        else:
            out["problems"].append("No SentinelOne site linked to this client.")
        if site and not token:
            out["problems"].append("SentinelOne didn't return a site token for the linked site.")
        if not installer:
            out["problems"].append("Set MSP_S1_INSTALLER_URL (a direct download link to the SentinelOne Windows "
                                   "installer you host) in .env.")
        out["parameters"] = {"SITETOKEN": token, "DOWNLOADURL": installer}
    elif client.av_vendor == "eset":
        out["script"] = ESET_SCRIPT
        eset_link = next((ln for ln in links if ln.platform == "eset"), None)
        pc = None
        if eset_link:
            pc = (await session.execute(select(PlatformCompany).where(
                PlatformCompany.platform == "eset",
                PlatformCompany.platform_company_id == eset_link.platform_company_id))).scalar_one_or_none()
        name = (pc.name if pc else None) or client.standard_name
        url, meta = "", {}
        try:
            from app.services.remediation import get_eset_installers
            usable = sorted((i for i in await get_eset_installers(name) if i.get("usable") and i.get("download_url")),
                            key=lambda i: i.get("create_time", ""), reverse=True)
            if usable:
                url, meta = usable[0]["download_url"], usable[0]
        except Exception as exc:  # noqa: BLE001
            out["problems"].append(f"ESET Connect installer lookup failed: {exc}")
        if not url:
            out["problems"].append(f"No usable ESET Live Installer found in ESET Connect for '{name}'.")
        out["eset_company"] = name
        out["installer"] = {k: meta.get(k) for k in ("display_name", "group_name", "create_time")} if meta else None
        out["parameters"] = {"INSTALLERURL": url}
    else:
        out["problems"].append("The client has no single antivirus vendor (none or both linked).")
    out["ready"] = not out["problems"]
    return out


# ── Verification (after each sweep) ───────────────────────────────────────


async def verify_review_items(session: AsyncSession) -> dict[str, int]:
    """For machines flagged under an approved change: AV now present → clear the flag; still missing → count."""
    from app.db.schema import ChangeEvent
    from app.services import vendor_client

    items = list((await session.execute(select(AvReviewItem).where(
        AvReviewItem.status.in_(("flagged", "deploy_sent"))))).scalars())
    if not items:
        return {"checked": 0, "fixed": 0, "not_fixed": 0}
    ctx = await _context(session)
    swept = {r.endpoint_id: r for r in ctx["swept"]}
    attribute_id = None
    fixed = not_fixed = 0
    now = datetime.now(UTC)
    for it in items:
        row = swept.get(it.endpoint_id)
        if row is None:
            continue
        found = set(row.vendors or [])
        if row.host_key in ctx["s1_hosts"].get(it.client_id, set()):
            found.add("sentinelone")
        if row.host_key in ctx["eset_hosts"]:
            found.add("eset")
        it.checks += 1
        it.last_checked_at = now
        if it.expected_av in found:
            if attribute_id is None:
                attribute_id = await flag_attribute_id()
            cleared = attribute_id and (await vendor_client._connector("asio").set_endpoint_custom_field(  # noqa: SLF001
                it.endpoint_id, attribute_id, ""))["ok"]
            it.status, it.cleared_at = "fixed", now if cleared else None
            it.note = "AV confirmed; flag cleared" if cleared else "AV confirmed; clearing the Asio flag failed"
            fixed += 1
            if it.change_id:
                session.add(ChangeEvent(id=str(uuid4()), change_id=it.change_id, event="verified", actor="av-sweep",
                                        details={"endpoint": it.hostname, "result": it.note}, at=now))
        elif it.status == "deploy_sent" and it.checks >= 2:
            it.status, it.note = "not_fixed", f"Still no {it.expected_av} after {it.checks} sweeps"
            not_fixed += 1
            if it.change_id:
                session.add(ChangeEvent(id=str(uuid4()), change_id=it.change_id, event="not_fixed", actor="av-sweep",
                                        details={"endpoint": it.hostname}, at=now))
    await session.commit()
    return {"checked": len(items), "fixed": fixed, "not_fixed": not_fixed}


async def flag_attribute_id() -> str | None:
    """Asio custom-field definition id for FLAG_FIELD (created once in the Asio console)."""
    from app.services import vendor_client

    for d in await vendor_client._connector("asio").get_custom_field_definitions():  # noqa: SLF001
        if str(d.get("name") or d.get("displayName") or "").strip().casefold() == FLAG_FIELD.casefold():
            return str(d.get("id") or d.get("definitionID") or d.get("attributeId") or "") or None
    return None
