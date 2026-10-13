"""ConnectWise Manage API Gateway service.

Wraps the CWM-API-Gateway-MCP's SQLite endpoint database and provides
a high-level async interface to discover and execute CW Manage API calls
using the project's existing credentials from ``app.config.Settings``.

References:
    https://github.com/jasondsmith72/CWM-API-Gateway-MCP
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import structlog

from app.config import get_settings

log = structlog.get_logger("cwm_gateway")

# ── Paths to the bundled SQLite databases ─────────────────────────
_TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools" / "cwm-gateway"
_API_DB_PATH = _TOOLS_DIR / "api_gateway" / "connectwise_api.db"
_FAST_MEMORY_DB_PATH = _TOOLS_DIR / "api_gateway" / "fast_memory_api.db"


# ── Auth helpers ──────────────────────────────────────────────────

def _psa_settings() -> Any:
    """The live PSA connector's settings (credentials saved in the app), else ``.env``."""
    try:
        from app.api.connector_health import get_connectors

        psa = next((c for c in get_connectors() if c.platform_name == "connectwise_psa"), None)
        if psa is not None and getattr(psa, "_settings", None) is not None:
            return psa._settings
    except Exception:  # noqa: BLE001 — MSP pack not loaded: fall back to .env
        pass
    return get_settings()


def _auth_headers() -> dict[str, str]:
    """Build CW Manage auth headers from the PSA connector's credentials."""
    s = _psa_settings()
    credentials = base64.b64encode(
        f"{s.cw_company_id}+{s.cw_public_key}:{s.cw_private_key}".encode()
    ).decode()
    return {
        "Authorization": f"Basic {credentials}",
        "clientId": s.cw_client_id,
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    return _psa_settings().cw_base_url


# ── Low-level API request ────────────────────────────────────────

async def api_request(
    method: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | list[dict[str, Any]] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Execute a ConnectWise Manage API request.

    Parameters
    ----------
    method : str
        HTTP method (GET, POST, PUT, PATCH, DELETE).
    endpoint : str
        API path, e.g. ``/service/tickets``.
    params : dict, optional
        Query parameters.
    data : dict or list, optional
        JSON body for POST/PUT/PATCH.  Lists are used for PATCH operations.
    timeout : float
        Request timeout in seconds.

    Returns
    -------
    dict or list
        Parsed JSON response.
    """
    url = f"{_base_url()}{endpoint}"
    headers = _auth_headers()
    log.debug("cwm_gateway.request", method=method, url=url, params=params)

    async with httpx.AsyncClient(timeout=timeout) as client:
        if method.upper() == "GET":
            resp = await client.get(url, headers=headers, params=params)
        elif method.upper() == "POST":
            resp = await client.post(url, headers=headers, json=data)
        elif method.upper() == "PUT":
            resp = await client.put(url, headers=headers, json=data)
        elif method.upper() == "PATCH":
            resp = await client.patch(url, headers=headers, json=data)
        elif method.upper() == "DELETE":
            resp = await client.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        resp.raise_for_status()
        return resp.json() if resp.content else {}


# ── Paginated GET helper ─────────────────────────────────────────

async def paginated_get(
    endpoint: str,
    params: dict[str, Any] | None = None,
    page_size: int = 250,
    max_pages: int = 40,
) -> list[dict[str, Any]]:
    """Fetch all pages from a CW Manage list endpoint."""
    params = dict(params) if params else {}
    params["pageSize"] = page_size
    all_results: list[dict[str, Any]] = []

    for page in range(1, max_pages + 1):
        params["page"] = page
        batch = await api_request("GET", endpoint, params=params)
        if not isinstance(batch, list):
            batch = [batch] if batch else []
        all_results.extend(batch)
        if len(batch) < page_size:
            break

    return all_results


# ── API Discovery (SQLite) ───────────────────────────────────────

def search_endpoints(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """Search the CW Manage API definition database for endpoints.

    Uses the pre-built SQLite database from CWM-API-Gateway-MCP.
    """
    if not _API_DB_PATH.exists():
        log.warning("cwm_gateway.db_missing", path=str(_API_DB_PATH))
        return []

    conn = sqlite3.connect(str(_API_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        # The CWM gateway DB has an 'endpoints' table with columns:
        #   path, method, summary, description, category, parameters, request_body, responses
        cur = conn.execute(
            """
            SELECT path, method, summary, description, category
            FROM endpoints
            WHERE path LIKE ? OR summary LIKE ? OR description LIKE ? OR category LIKE ?
            LIMIT ?
            """,
            (f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%", max_results),
        )
        return [dict(row) for row in cur.fetchall()]
    except sqlite3.OperationalError as exc:
        log.warning("cwm_gateway.search_error", error=str(exc))
        return []
    finally:
        conn.close()


def get_endpoint_details(path: str, method: str = "GET") -> dict[str, Any] | None:
    """Get full details for a specific API endpoint."""
    if not _API_DB_PATH.exists():
        return None

    conn = sqlite3.connect(str(_API_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT * FROM endpoints WHERE path = ? AND method = ?",
            (path, method.upper()),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def list_categories() -> list[str]:
    """List all API categories from the database."""
    if not _API_DB_PATH.exists():
        return []

    conn = sqlite3.connect(str(_API_DB_PATH))
    try:
        cur = conn.execute("SELECT DISTINCT category FROM endpoints ORDER BY category")
        return [row[0] for row in cur.fetchall() if row[0]]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# ── High-level ticket helpers ────────────────────────────────────

async def get_tickets(
    conditions: str = "closedFlag=false",
    page_size: int = 100,
    fields: str | None = None,
    order_by: str = "id desc",
) -> list[dict[str, Any]]:
    """Fetch service tickets from CW Manage."""
    params: dict[str, Any] = {
        "conditions": conditions,
        "orderBy": order_by,
    }
    if fields:
        params["fields"] = fields
    return await paginated_get("/service/tickets", params=params, page_size=page_size)


async def get_ticket(ticket_id: int) -> dict[str, Any]:
    """Fetch a single service ticket by ID."""
    return await api_request("GET", f"/service/tickets/{ticket_id}")


async def create_ticket(ticket_data: dict[str, Any]) -> dict[str, Any]:
    """Create a new service ticket."""
    return await api_request("POST", "/service/tickets", data=ticket_data)


async def update_ticket(ticket_id: int, operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Update a service ticket using PATCH operations."""
    return await api_request("PATCH", f"/service/tickets/{ticket_id}", data=operations)


async def get_boards() -> list[dict[str, Any]]:
    """Fetch all service boards."""
    return await paginated_get("/service/boards")


async def get_members(
    conditions: str = "inactiveFlag=false",
) -> list[dict[str, Any]]:
    """Fetch system members (technicians)."""
    return await paginated_get("/system/members", params={"conditions": conditions})


async def get_statuses(board_id: int) -> list[dict[str, Any]]:
    """Fetch statuses for a service board."""
    return await api_request("GET", f"/service/boards/{board_id}/statuses")


async def get_priorities() -> list[dict[str, Any]]:
    """Fetch all ticket priorities."""
    return await paginated_get("/service/priorities")


async def get_ticket_notes(ticket_id: int) -> list[dict[str, Any]]:
    """Fetch all notes for a service ticket."""
    return await paginated_get(f"/service/tickets/{ticket_id}/notes")


async def add_ticket_note(
    ticket_id: int, text: str, internal: bool = True
) -> dict[str, Any]:
    """Add a note to a service ticket."""
    return await api_request(
        "POST",
        f"/service/tickets/{ticket_id}/notes",
        data={
            "text": text,
            "internalAnalysisFlag": internal,
            "detailDescriptionFlag": not internal,
        },
    )


# ── Time entry helpers ───────────────────────────────────────────

async def get_work_types() -> list[dict[str, Any]]:
    """Fetch all work types available for time entries."""
    return await paginated_get("/time/workTypes")


async def get_member_default_work_type(member_id: int) -> dict[str, Any] | None:
    """Fetch a member's default work type, if configured."""
    try:
        member = await api_request("GET", f"/system/members/{member_id}")
        work_type = member.get("defaultTimeEntryWorkType")
        return work_type if work_type and isinstance(work_type, dict) else None
    except Exception:
        return None


async def get_time_entries(ticket_id: int) -> list[dict[str, Any]]:
    """Fetch all time entries charged to a service ticket."""
    return await paginated_get(
        "/time/entries",
        params={"conditions": f'chargeToId={ticket_id} AND chargeToType="ServiceTicket"'},
    )


async def add_time_entry(
    ticket_id: int,
    member_id: int,
    actual_hours: float,
    time_start: str,
    notes: str = "",
    billable_option: str = "Billable",
    work_type_id: int | None = None,
) -> dict[str, Any]:
    """Add a time entry to a service ticket.

    Parameters
    ----------
    ticket_id : int
        The ticket to charge time against.
    member_id : int
        ID of the technician/member performing the work.
    actual_hours : float
        Hours worked (e.g. 1.5 for 1h 30m).
    time_start : str
        ISO date or datetime string for when the work started (e.g. ``"2024-01-15"``).
    notes : str
        Optional work notes.
    billable_option : str
        One of ``"Billable"``, ``"DoNotBill"``, ``"NoCharge"``, ``"NoDefault"``.
    work_type_id : int | None
        Optional work type ID. If None, CWM will use the member's default work type.
    """
    data: dict[str, Any] = {
        "chargeToId": ticket_id,
        "chargeToType": "ServiceTicket",
        "member": {"id": member_id},
        "actualHours": actual_hours,
        "timeStart": time_start,
        "notes": notes,
        "billableOption": billable_option,
    }
    if work_type_id is not None:
        data["workType"] = {"id": work_type_id}

    return await api_request("POST", "/time/entries", data=data)
