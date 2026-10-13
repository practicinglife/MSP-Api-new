"""Actions API — scan, alerts, tickets, and remote commands across platforms."""

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.connector_health import get_connectors
from app.auth.write_mode import enforce_direct_write

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/actions", tags=["actions"])


# ---- Request models ----

class ScanRequestBody(BaseModel):
    """POST body for triggering a scan."""

    platform: str
    device_ids: list[str]


class CommandRequestBody(BaseModel):
    """POST body for running a remote command."""

    platform: str
    device_ids: list[str]
    command: str


# ---- Helpers ----

def _find_connector(platform: str):  # type: ignore[no-untyped-def]
    for c in get_connectors():
        if c.platform_name == platform:
            return c
    return None


# ---- Capabilities ----

@router.get("/capabilities", response_model=list[dict[str, Any]])
async def list_capabilities() -> list[dict[str, Any]]:
    """Return each connector's supported action capabilities."""
    return [
        {
            "platform": c.platform_name,
            "capabilities": c.capabilities(),
            "configured": len(c.check_config()) == 0,
        }
        for c in get_connectors()
    ]


# ---- Scans ----

@router.post("/scan", response_model=dict[str, Any])
async def trigger_scan(body: ScanRequestBody) -> dict[str, Any]:
    """Trigger an antivirus/EDR scan on the specified platform and devices."""
    await enforce_direct_write(body.platform)
    connector = _find_connector(body.platform)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Platform not found: {body.platform}")
    if not connector.supports_scan:
        raise HTTPException(status_code=400, detail=f"{body.platform} does not support scans")

    try:
        await connector.authenticate()
        result = await connector.initiate_scan(body.device_ids)
        log.info("actions.scan.triggered", platform=body.platform, devices=len(body.device_ids))
        return {"status": "ok", "platform": body.platform, "result": result}
    except Exception as exc:  # noqa: BLE001
        log.error("actions.scan.error", platform=body.platform, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


# ---- Alerts / Threats ----

@router.get("/alerts", response_model=list[dict[str, Any]])
async def get_all_alerts(limit: int = 50) -> list[dict[str, Any]]:
    """Aggregate security alerts from all platforms that support them."""
    all_alerts: list[dict[str, Any]] = []
    for connector in get_connectors():
        if not connector.supports_alerts:
            continue
        if connector.check_config():
            continue
        try:
            await connector.authenticate()
            alerts = await connector.get_alerts(limit=limit)
            if alerts:
                all_alerts.extend(alerts)
        except Exception as exc:  # noqa: BLE001
            log.warning("actions.alerts.error", platform=connector.platform_name, error=str(exc))
            all_alerts.append({
                "platform": connector.platform_name,
                "alert_id": "",
                "severity": "error",
                "title": f"Failed to fetch: {exc}",
                "description": "",
                "device_hostname": "",
                "device_id": "",
                "company_name": "",
                "created_at": "",
            })
    return all_alerts


@router.get("/alerts/{platform}", response_model=list[dict[str, Any]])
async def get_platform_alerts(
    platform: str, limit: int = 50, severity: str | None = None
) -> list[dict[str, Any]]:
    """Return security alerts from a specific platform."""
    connector = _find_connector(platform)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Platform not found: {platform}")
    if not connector.supports_alerts:
        raise HTTPException(status_code=400, detail=f"{platform} does not support alerts")

    try:
        await connector.authenticate()
        alerts = await connector.get_alerts(limit=limit, severity=severity)
        return alerts or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


# ---- Tickets ----

@router.get("/tickets", response_model=list[dict[str, Any]])
async def get_all_tickets(limit: int = 50) -> list[dict[str, Any]]:
    """Aggregate service tickets from all PSA platforms."""
    all_tickets: list[dict[str, Any]] = []
    for connector in get_connectors():
        if not connector.supports_tickets:
            continue
        if connector.check_config():
            continue
        try:
            await connector.authenticate()
            tickets = await connector.get_tickets(limit=limit)
            if tickets:
                for t in tickets:
                    t["platform"] = connector.platform_name
                all_tickets.extend(tickets)
        except Exception as exc:  # noqa: BLE001
            log.warning("actions.tickets.error", platform=connector.platform_name, error=str(exc))
    return all_tickets


@router.get("/tickets/{platform}", response_model=list[dict[str, Any]])
async def get_platform_tickets(
    platform: str, company_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Return service tickets from a specific PSA platform."""
    connector = _find_connector(platform)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Platform not found: {platform}")
    if not connector.supports_tickets:
        raise HTTPException(status_code=400, detail=f"{platform} does not support tickets")

    try:
        await connector.authenticate()
        tickets = await connector.get_tickets(company_id=company_id, limit=limit)
        return tickets or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


# ---- Remote commands ----

@router.post("/command", response_model=dict[str, Any])
async def run_remote_command(body: CommandRequestBody) -> dict[str, Any]:
    """Run a remote command on the specified platform and devices."""
    await enforce_direct_write(body.platform)
    connector = _find_connector(body.platform)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Platform not found: {body.platform}")
    if not connector.supports_install:
        raise HTTPException(
            status_code=400,
            detail=f"{body.platform} does not support remote commands yet",
        )

    try:
        await connector.authenticate()
        result = await connector.run_command(body.device_ids, body.command)
        return {"status": "ok", "platform": body.platform, "result": result}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))
