"""Unified API Proxy — forwards requests to any configured platform.

Provides ``/api/proxy/{platform}/{path:path}`` routes that handle
authentication, pagination, and method dispatch automatically.
Each connector's auth pattern is resolved transparently so callers
just need to know the platform name and the API path.

Also provides ``/api/proxy/catalog`` for high-value endpoint discovery
that agents / workflows can use to find the most useful operations.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.api.connector_health import get_connectors
from app.db.repository import ApiDiscoveryRepository, get_session_factory
from app.auth.write_mode import enforce_direct_write
from app.services.cwm_gateway import api_request as cwm_api_request

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/proxy", tags=["proxy"])
catalog_router = APIRouter(prefix="/api/catalog", tags=["catalog"])

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


# ── Auth resolution ──────────────────────────────────────────────

def _resolve_auth(connector: Any) -> tuple[str, dict[str, str]]:
    """Return (base_url, headers) for a connector, handling each auth pattern.

    Supported patterns:
    - SentinelOne: ApiToken header
    - ConnectWise PSA: Basic auth + clientId  (routed through cwm_gateway)
    - Asio: OAuth2 Bearer (must call authenticate() first)
    - ScreenConnect: CTRLAuthHeader
    - ESET: Bearer token (must call authenticate() first)
    - GenericRest: configurable auth
    """
    base_url = ""
    headers: dict[str, str] = {}

    # Get base URL
    if hasattr(connector, "_base_url"):
        base_url = connector._base_url()
        if callable(base_url):
            base_url = base_url()
    elif hasattr(connector, "_settings"):
        for attr in ("s1_base_url", "eset_base_url", "screenconnect_base_url"):
            val = getattr(connector._settings, attr, "")
            if val:
                base_url = val.rstrip("/")
                break

    # Get auth headers
    if hasattr(connector, "_auth_headers") and callable(connector._auth_headers):
        headers = connector._auth_headers()
    elif hasattr(connector, "_access_token") and connector._access_token:
        headers["Authorization"] = f"Bearer {connector._access_token}"
    elif hasattr(connector, "_settings"):
        s = connector._settings
        if hasattr(s, "s1_api_token") and s.s1_api_token:
            headers["Authorization"] = f"ApiToken {s.s1_api_token}"

    return base_url, headers


# ── CW PSA special routing ───────────────────────────────────────

async def _proxy_cwm(method: str, path: str, params: dict | None, body: Any) -> dict[str, Any]:
    """Route CW PSA calls through cwm_gateway for correct auth + pagination."""
    # Ensure path starts with /
    if not path.startswith("/"):
        path = f"/{path}"

    start = time.monotonic()
    try:
        result = await cwm_api_request(method, path, params=params, data=body)
        latency = int((time.monotonic() - start) * 1000)
        return {
            "platform": "connectwise_psa",
            "path": path,
            "method": method,
            "source": "live",
            "status_code": 200,
            "latency_ms": latency,
            "response": result,
        }
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=exc.response.text[:500],
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ── Generic proxy execution ──────────────────────────────────────

async def _proxy_generic(
    connector: Any,
    platform: str,
    method: str,
    path: str,
    params: dict | None,
    body: Any,
) -> dict[str, Any]:
    """Proxy a request to any non-CWM platform."""
    # Authenticate if needed (Asio OAuth2, ESET session, etc.)
    try:
        await connector.authenticate()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Authentication failed: {exc}")

    base_url, headers = _resolve_auth(connector)
    if not base_url:
        raise HTTPException(status_code=400, detail=f"Cannot determine base URL for '{platform}'")

    # Asio: API calls go through /api sub-path
    if platform == "asio" and hasattr(connector, "_api_base"):
        base_url = connector._api_base()

    if not path.startswith("/"):
        path = f"/{path}"
    url = f"{base_url}{path}"

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, verify=False) as client:  # noqa: S501
            if method == "GET":
                resp = await client.get(url, headers=headers, params=params)
            elif method == "POST":
                resp = await client.post(url, headers=headers, json=body, params=params)
            elif method == "PUT":
                resp = await client.put(url, headers=headers, json=body, params=params)
            elif method == "PATCH":
                resp = await client.patch(url, headers=headers, json=body, params=params)
            elif method == "DELETE":
                resp = await client.delete(url, headers=headers, params=params)
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported method: {method}")

            latency = int((time.monotonic() - start) * 1000)

            try:
                resp_body = resp.json()
            except Exception:
                resp_body = {"raw": resp.text[:5000]}

            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=resp_body)

            return {
                "platform": platform,
                "path": path,
                "method": method,
                "source": "live",
                "status_code": resp.status_code,
                "latency_ms": latency,
                "response": resp_body,
            }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"API call failed: {exc}")


# ── Proxy routes ─────────────────────────────────────────────────

@router.api_route(
    "/{platform}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    summary="Proxy any request to a configured platform API",
)
async def proxy_request(
    platform: str,
    path: str,
    request: Request,
) -> dict[str, Any]:
    """Forward a request to the specified platform with automatic auth.

    Examples::

        GET  /api/proxy/sentinelone/web/api/v2.1/agents?limit=5
        GET  /api/proxy/connectwise_psa/company/companies?conditions=name like '%accurate%'
        POST /api/proxy/asio/companies
        GET  /api/proxy/eset/api/Companies
        GET  /api/proxy/screenconnect/...
    """
    method = request.method.upper()
    if method != "GET":
        await enforce_direct_write(platform)

    # Parse params from query string
    params: dict[str, str] = dict(request.query_params)

    # Parse body for POST/PUT/PATCH
    body: Any = None
    if method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
        except Exception:
            body = None

    return await vendor_request(platform, method, path, params, body)


async def vendor_request(platform: str, method: str, path: str, params: dict | None, body: Any) -> dict[str, Any]:
    """Call a built-in platform's API with that platform's own sign-in. The caller enforces write rules
    (the proxy route above, or the connection call path for built-in connections)."""
    # CW PSA — route through cwm_gateway for correct auth
    if platform == "connectwise_psa":
        return await _proxy_cwm(method, path, params, body)

    # Find connector
    connectors = get_connectors()
    connector = next((c for c in connectors if c.platform_name == platform), None)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Connector '{platform}' not found or not configured")

    return await _proxy_generic(connector, platform, method, path, params, body)


# ── High-value endpoint catalog ──────────────────────────────────

# Curated list of the most useful endpoints across all platforms.
# Agents/workflows can query this to discover what operations to use.
# Each entry maps to a real API path on the platform.

CURATED_ENDPOINTS: list[dict[str, Any]] = [
    # ── SentinelOne ──
    {"platform": "sentinelone", "category": "devices", "action": "list_agents",
     "method": "GET", "path": "/web/api/v2.1/agents", "description": "List all S1 agents with filtering",
     "useful_params": {"limit": 100, "computerName__contains": "", "siteIds": ""},
     "workflow_trigger": "device_audit"},
    {"platform": "sentinelone", "category": "devices", "action": "get_agent",
     "method": "GET", "path": "/web/api/v2.1/agents", "description": "Get a specific agent by ID",
     "useful_params": {"ids": "<agent_id>"},
     "workflow_trigger": "device_detail"},
    {"platform": "sentinelone", "category": "security", "action": "list_threats",
     "method": "GET", "path": "/web/api/v2.1/threats", "description": "List active threats/alerts",
     "useful_params": {"limit": 50, "resolved": "false"},
     "workflow_trigger": "incident_response"},
    {"platform": "sentinelone", "category": "security", "action": "initiate_scan",
     "method": "POST", "path": "/web/api/v2.1/agents/actions/initiate-scan", "description": "Trigger full disk scan",
     "useful_params": {},
     "workflow_trigger": "security_scan"},
    {"platform": "sentinelone", "category": "lifecycle", "action": "decommission_agent",
     "method": "POST", "path": "/web/api/v2.1/agents/actions/decommission", "description": "Decommission an agent",
     "useful_params": {},
     "workflow_trigger": "device_offboard"},
    {"platform": "sentinelone", "category": "sites", "action": "list_sites",
     "method": "GET", "path": "/web/api/v2.1/sites", "description": "List all S1 sites (companies)",
     "useful_params": {"limit": 100},
     "workflow_trigger": "company_audit"},

    # ── ConnectWise PSA ──
    {"platform": "connectwise_psa", "category": "tickets", "action": "list_tickets",
     "method": "GET", "path": "/service/tickets", "description": "List service tickets with conditions",
     "useful_params": {"conditions": "closedFlag=false", "pageSize": 100},
     "workflow_trigger": "ticket_management"},
    {"platform": "connectwise_psa", "category": "tickets", "action": "create_ticket",
     "method": "POST", "path": "/service/tickets", "description": "Create a new service ticket",
     "useful_params": {},
     "workflow_trigger": "incident_response"},
    {"platform": "connectwise_psa", "category": "tickets", "action": "update_ticket",
     "method": "PATCH", "path": "/service/tickets/{id}", "description": "Update ticket fields (JSON Patch array)",
     "useful_params": {},
     "workflow_trigger": "ticket_management"},
    {"platform": "connectwise_psa", "category": "companies", "action": "list_companies",
     "method": "GET", "path": "/company/companies", "description": "List all CW companies",
     "useful_params": {"pageSize": 250},
     "workflow_trigger": "company_audit"},
    {"platform": "connectwise_psa", "category": "devices", "action": "list_configurations",
     "method": "GET", "path": "/company/configurations", "description": "List all managed configurations (devices)",
     "useful_params": {"pageSize": 250},
     "workflow_trigger": "device_audit"},
    {"platform": "connectwise_psa", "category": "devices", "action": "update_configuration",
     "method": "PATCH", "path": "/company/configurations/{id}", "description": "Update a configuration (JSON Patch)",
     "useful_params": {},
     "workflow_trigger": "device_lifecycle"},
    {"platform": "connectwise_psa", "category": "agreements", "action": "list_agreements",
     "method": "GET", "path": "/finance/agreements", "description": "List finance agreements",
     "useful_params": {"conditions": "cancelledFlag=false", "pageSize": 100},
     "workflow_trigger": "billing_audit"},
    {"platform": "connectwise_psa", "category": "members", "action": "list_members",
     "method": "GET", "path": "/system/members", "description": "List team members (technicians)",
     "useful_params": {"pageSize": 100},
     "workflow_trigger": "team_management"},
    {"platform": "connectwise_psa", "category": "time", "action": "create_time_entry",
     "method": "POST", "path": "/time/entries", "description": "Log time against a ticket",
     "useful_params": {},
     "workflow_trigger": "time_tracking"},

    # ── Asio ──
    {"platform": "asio", "category": "companies", "action": "list_companies",
     "method": "GET", "path": "/companies", "description": "List Asio companies",
     "useful_params": {},
     "workflow_trigger": "company_audit"},
    {"platform": "asio", "category": "devices", "action": "list_endpoints",
     "method": "GET", "path": "/endpoints", "description": "List Asio endpoints (devices)",
     "useful_params": {},
     "workflow_trigger": "device_audit"},
    {"platform": "asio", "category": "automation", "action": "list_scripts",
     "method": "GET", "path": "/automation/scripts", "description": "List available automation scripts",
     "useful_params": {},
     "workflow_trigger": "remediation"},
    {"platform": "asio", "category": "automation", "action": "run_script",
     "method": "POST", "path": "/automation/scripts/run", "description": "Execute an automation script on endpoints",
     "useful_params": {},
     "workflow_trigger": "remediation"},

    # ── ESET ──
    {"platform": "eset", "category": "companies", "action": "list_companies",
     "method": "GET", "path": "/api/Companies", "description": "List ESET MSP companies",
     "useful_params": {},
     "workflow_trigger": "company_audit"},
    {"platform": "eset", "category": "devices", "action": "list_devices",
     "method": "GET", "path": "/api/Devices", "description": "List ESET managed devices",
     "useful_params": {},
     "workflow_trigger": "device_audit"},

    # ── ScreenConnect ──
    {"platform": "screenconnect", "category": "sessions", "action": "list_sessions",
     "method": "GET", "path": "/Services/PageService.ashx/GetHostSessionInfo", "description": "List active SC sessions",
     "useful_params": {},
     "workflow_trigger": "device_audit"},

    # ── MSP Tools (self) ──
    {"platform": "msp_api_tools", "category": "reconciliation", "action": "run_reconciliation",
     "method": "POST", "path": "/api/reconcile", "description": "Run full cross-platform device reconciliation",
     "useful_params": {},
     "workflow_trigger": "daily_health_check"},
    {"platform": "msp_api_tools", "category": "breakdown", "action": "get_breakdown",
     "method": "GET", "path": "/api/companies/reconciled", "description": "Get per-company matched/mismatched breakdown",
     "useful_params": {},
     "workflow_trigger": "device_audit"},
    {"platform": "msp_api_tools", "category": "companies", "action": "company_summary",
     "method": "GET", "path": "/api/companies/summary", "description": "Cross-platform company summary with device counts",
     "useful_params": {},
     "workflow_trigger": "company_audit"},
    {"platform": "msp_api_tools", "category": "connectors", "action": "test_connectors",
     "method": "POST", "path": "/api/connectors/test", "description": "Test all connector connectivity",
     "useful_params": {},
     "workflow_trigger": "daily_health_check"},
]


@catalog_router.get("", summary="Curated high-value endpoints for agents/workflows")
async def get_catalog(
    platform: str | None = Query(None, description="Filter by platform"),
    category: str | None = Query(None, description="Filter by category (devices, tickets, security, etc.)"),
    trigger: str | None = Query(None, description="Filter by workflow trigger name"),
) -> list[dict[str, Any]]:
    """Return curated high-value endpoints that agents and workflows should use.

    Each entry includes the platform, method, path, description, and a
    ``workflow_trigger`` tag for mapping to automation workflows.
    """
    results = CURATED_ENDPOINTS
    if platform:
        results = [e for e in results if e["platform"] == platform]
    if category:
        results = [e for e in results if e["category"] == category]
    if trigger:
        results = [e for e in results if e.get("workflow_trigger") == trigger]
    return results


@catalog_router.get("/triggers", summary="List all workflow trigger names")
async def get_workflow_triggers() -> list[dict[str, Any]]:
    """Return all unique workflow triggers with their endpoint counts."""
    triggers: dict[str, list[str]] = {}
    for ep in CURATED_ENDPOINTS:
        t = ep.get("workflow_trigger", "")
        if t:
            triggers.setdefault(t, []).append(f"{ep['platform']}/{ep['action']}")
    return [
        {"trigger": t, "endpoint_count": len(eps), "endpoints": eps}
        for t, eps in sorted(triggers.items())
    ]


@catalog_router.get("/categories", summary="List all endpoint categories")
async def get_categories() -> list[dict[str, Any]]:
    """Return all categories with their platform breakdown."""
    cats: dict[str, dict[str, int]] = {}
    for ep in CURATED_ENDPOINTS:
        c = ep["category"]
        p = ep["platform"]
        cats.setdefault(c, {})
        cats[c][p] = cats[c].get(p, 0) + 1
    return [
        {"category": c, "platforms": plats, "total": sum(plats.values())}
        for c, plats in sorted(cats.items())
    ]
