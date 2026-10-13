"""Ticket management API — list, create, triage, and route CW Manage tickets.

Combines the CWM Gateway (API discovery + execution) with the ticket
triage engine (classification + routing) to provide a unified ticket
management interface.

Routes:
    GET  /api/tickets              — List open tickets
    GET  /api/tickets/{id}         — Get ticket details
    POST /api/tickets              — Create a new ticket
    PATCH /api/tickets/{id}        — Update ticket fields (status, priority, owner …)
    POST /api/tickets/triage       — Classify & assess urgency of text
    GET  /api/tickets/{id}/notes   — List notes for a ticket
    POST /api/tickets/{id}/notes   — Add a note to a ticket
    GET  /api/tickets/boards       — List service boards
    GET  /api/tickets/boards/{id}/statuses — List statuses for a board
    GET  /api/tickets/members      — List technicians
    GET  /api/tickets/priorities   — List priorities
    GET  /api/tickets/api-search   — Search CW Manage API endpoints
    POST /api/tickets/api-execute  — Execute an arbitrary CW Manage API call
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.auth.write_mode import enforce_direct_write
from app.services import cwm_gateway
from app.services.ticket_triage import triage_ticket

log = structlog.get_logger("api.tickets")
router = APIRouter(prefix="/api/tickets", tags=["tickets"])


# ── Request / response models ────────────────────────────────────

class TriageRequest(BaseModel):
    summary: str
    description: str = ""


class CreateTicketRequest(BaseModel):
    summary: str
    description: str = ""
    company_id: int | None = None
    board_id: int | None = None
    priority_id: int | None = None
    status_id: int | None = None
    auto_triage: bool = True


class AddNoteRequest(BaseModel):
    text: str
    internal: bool = True


class AddTimeEntryRequest(BaseModel):
    member_id: int
    actual_hours: float
    time_start: str          # ISO date: "2024-01-15"
    notes: str = ""
    billable_option: str = "Billable"  # Billable | DoNotBill | NoCharge | NoDefault
    work_type_id: int | None = None


class ApiExecuteRequest(BaseModel):
    method: str = "GET"
    endpoint: str
    params: dict[str, Any] | None = None
    data: dict[str, Any] | None = None


class UpdateTicketRequest(BaseModel):
    """Fields to update on an existing ticket via PATCH operations."""
    status_id: int | None = None
    priority_id: int | None = None
    owner_id: int | None = None
    summary: str | None = None
    board_id: int | None = None
    closed_flag: bool | None = None


# ── Ticket CRUD ──────────────────────────────────────────────────

@router.get("", response_model=list[dict[str, Any]])
async def list_tickets(
    conditions: str = Query("closedFlag=false", description="CW Manage conditions string"),
    limit: int = Query(100, ge=1, le=1000),
    order_by: str = Query("id desc"),
) -> list[dict[str, Any]]:
    """List ConnectWise Manage service tickets."""
    try:
        raw = await cwm_gateway.get_tickets(
            conditions=conditions,
            page_size=limit,
            order_by=order_by,
        )
        # Normalise to a consistent shape
        results = []
        for t in raw:
            company = t.get("company", {}) or {}
            priority = t.get("priority", {}) or {}
            status = t.get("status", {}) or {}
            board = t.get("board", {}) or {}
            owner = t.get("owner", {}) or {}
            results.append({
                "id": t.get("id"),
                "summary": t.get("summary", ""),
                "status": status.get("name", ""),
                "priority": priority.get("name", ""),
                "board": board.get("name", ""),
                "company_name": company.get("name", ""),
                "company_id": company.get("id"),
                "assigned_to": owner.get("name", "") if owner else "",
                "created_at": t.get("_info", {}).get("dateEntered", ""),
                "updated_at": t.get("_info", {}).get("lastUpdated", ""),
            })
        log.info("tickets.list", count=len(results))
        return results
    except Exception as exc:
        log.error("tickets.list_error", error=str(exc))
        raise HTTPException(status_code=502, detail=f"CW Manage API error: {exc}")


@router.get("/boards", response_model=list[dict[str, Any]])
async def list_boards() -> list[dict[str, Any]]:
    """List available service boards."""
    try:
        return await cwm_gateway.get_boards()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/members", response_model=list[dict[str, Any]])
async def list_members() -> list[dict[str, Any]]:
    """List active CW Manage members (technicians)."""
    try:
        raw = await cwm_gateway.get_members()
        return [
            {
                "id": m.get("id"),
                "identifier": m.get("identifier", ""),
                "name": f"{m.get('firstName', '')} {m.get('lastName', '')}".strip(),
                "email": m.get("officeEmail", ""),
                "title": m.get("title", ""),
            }
            for m in raw
        ]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/priorities", response_model=list[dict[str, Any]])
async def list_priorities() -> list[dict[str, Any]]:
    """List available ticket priorities."""
    try:
        return await cwm_gateway.get_priorities()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/work-types", response_model=list[dict[str, Any]])
async def list_work_types() -> list[dict[str, Any]]:
    """List all work types available for time entries."""
    try:
        return await cwm_gateway.get_work_types()
    except Exception as exc:
        log.error("tickets.work_types_error", error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/members/{member_id}/default-work-type", response_model=dict[str, Any] | None)
async def get_member_default_work_type(member_id: int) -> dict[str, Any] | None:
    """Get a member's default work type."""
    try:
        return await cwm_gateway.get_member_default_work_type(member_id)
    except Exception as exc:
        log.error("tickets.member_default_work_type_error", member_id=member_id, error=str(exc))
        return None


@router.get("/{ticket_id}", response_model=dict[str, Any])
async def get_ticket(ticket_id: int) -> dict[str, Any]:
    """Get a single ticket by ID."""
    try:
        return await cwm_gateway.get_ticket(ticket_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("", response_model=dict[str, Any], status_code=201)
async def create_ticket(req: CreateTicketRequest) -> dict[str, Any]:
    """Create a new service ticket in CW Manage.

    If ``auto_triage`` is True, the ticket summary/description will be
    classified and the triage result added as an internal note.
    """
    await enforce_direct_write("connectwise_psa")
    ticket_data: dict[str, Any] = {
        "summary": req.summary,
    }
    if req.description:
        ticket_data["initialDescription"] = req.description
    if req.company_id:
        ticket_data["company"] = {"id": req.company_id}
    if req.board_id:
        ticket_data["board"] = {"id": req.board_id}
    if req.priority_id:
        ticket_data["priority"] = {"id": req.priority_id}
    if req.status_id:
        ticket_data["status"] = {"id": req.status_id}

    try:
        created = await cwm_gateway.create_ticket(ticket_data)
        ticket_id = created.get("id")

        # Auto-triage and add note
        if req.auto_triage and ticket_id:
            triage_result = triage_ticket(req.summary, req.description)
            note_text = (
                f"[Auto-Triage]\n"
                f"Category: {triage_result['category']}\n"
                f"Urgency: {triage_result['urgency']}\n"
                f"Confidence: {triage_result['confidence']}\n"
                f"Triggers: {', '.join(triage_result.get('triggers', []))}"
            )
            try:
                await cwm_gateway.add_ticket_note(ticket_id, note_text, internal=True)
            except Exception:
                log.warning("tickets.triage_note_failed", ticket_id=ticket_id)

        log.info("tickets.created", ticket_id=ticket_id)
        return {**created, "triage": triage_result if req.auto_triage else None}
    except Exception as exc:
        log.error("tickets.create_error", error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/{ticket_id}/notes", response_model=dict[str, Any])
async def add_note(ticket_id: int, req: AddNoteRequest) -> dict[str, Any]:
    """Add a note to an existing ticket."""
    await enforce_direct_write("connectwise_psa")
    try:
        return await cwm_gateway.add_ticket_note(ticket_id, req.text, req.internal)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/{ticket_id}/notes", response_model=list[dict[str, Any]])
async def list_notes(ticket_id: int) -> list[dict[str, Any]]:
    """List all notes for a ticket."""
    try:
        raw = await cwm_gateway.get_ticket_notes(ticket_id)
        results = []
        for n in raw:
            member = n.get("member") or n.get("contact") or {}
            results.append({
                "id": n.get("id"),
                "text": n.get("text", ""),
                "internal": n.get("internalAnalysisFlag", False),
                "created_by": member.get("name", ""),
                "created_at": n.get("_info", {}).get("dateEntered", ""),
            })
        return results
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ── Time entries ─────────────────────────────────────────────────

@router.get("/{ticket_id}/time", response_model=list[dict[str, Any]])
async def list_time_entries(ticket_id: int) -> list[dict[str, Any]]:
    """List all time entries charged to a ticket."""
    try:
        raw = await cwm_gateway.get_time_entries(ticket_id)
        results = []
        for e in raw:
            member = e.get("member") or {}
            results.append({
                "id": e.get("id"),
                "member_id": member.get("id"),
                "member_name": member.get("name", ""),
                "actual_hours": e.get("actualHours", 0),
                "time_start": e.get("timeStart", ""),
                "notes": e.get("notes", ""),
                "billable_option": e.get("billableOption", ""),
            })
        return results
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/{ticket_id}/time", response_model=dict[str, Any], status_code=201)
async def add_time_entry(ticket_id: int, req: AddTimeEntryRequest) -> dict[str, Any]:
    """Add a time entry to a ticket."""
    await enforce_direct_write("connectwise_psa")
    try:
        result = await cwm_gateway.add_time_entry(
            ticket_id=ticket_id,
            member_id=req.member_id,
            actual_hours=req.actual_hours,
            time_start=req.time_start,
            notes=req.notes,
            billable_option=req.billable_option,
            work_type_id=req.work_type_id,
        )
        log.info("tickets.time_entry_added", ticket_id=ticket_id, hours=req.actual_hours, member=req.member_id)
        return result
    except Exception as exc:
        log.error("tickets.time_entry_error", ticket_id=ticket_id, error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc))


@router.patch("/{ticket_id}", response_model=dict[str, Any])
async def update_ticket(ticket_id: int, req: UpdateTicketRequest) -> dict[str, Any]:
    """Update ticket fields (status, priority, owner, etc.) via PATCH operations."""
    await enforce_direct_write("connectwise_psa")
    operations: list[dict[str, Any]] = []
    if req.status_id is not None:
        operations.append({"op": "replace", "path": "status/id", "value": req.status_id})
    if req.priority_id is not None:
        operations.append({"op": "replace", "path": "priority/id", "value": req.priority_id})
    if req.owner_id is not None:
        operations.append({"op": "replace", "path": "owner/id", "value": req.owner_id})
    if req.summary is not None:
        operations.append({"op": "replace", "path": "summary", "value": req.summary})
    if req.board_id is not None:
        operations.append({"op": "replace", "path": "board/id", "value": req.board_id})
    if req.closed_flag is not None:
        operations.append({"op": "replace", "path": "closedFlag", "value": req.closed_flag})

    if not operations:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        result = await cwm_gateway.update_ticket(ticket_id, operations)
        log.info("tickets.updated", ticket_id=ticket_id, ops=len(operations))
        return result
    except Exception as exc:
        log.error("tickets.update_error", ticket_id=ticket_id, error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/boards/{board_id}/statuses", response_model=list[dict[str, Any]])
async def list_board_statuses(board_id: int) -> list[dict[str, Any]]:
    """List statuses available for a given service board."""
    try:
        raw = await cwm_gateway.get_statuses(board_id)
        if isinstance(raw, dict):
            raw = [raw]
        return [
            {
                "id": s.get("id"),
                "name": s.get("name", ""),
                "closed_flag": s.get("closedStatus", False),
                "default_flag": s.get("defaultFlag", False),
                "sort_order": s.get("sortOrder", 0),
            }
            for s in raw
        ]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ── Triage endpoint ──────────────────────────────────────────────

@router.post("/triage", response_model=dict[str, Any])
async def triage_endpoint(req: TriageRequest) -> dict[str, Any]:
    """Classify and assess urgency of ticket text without creating a ticket."""
    return triage_ticket(req.summary, req.description)


# ── CW Manage API Discovery & Execution ──────────────────────────

@router.get("/api-search", response_model=list[dict[str, Any]])
async def search_api(
    q: str = Query(..., description="Search query for CW Manage API endpoints"),
    max_results: int = Query(10, ge=1, le=50),
) -> list[dict[str, Any]]:
    """Search the ConnectWise Manage API endpoint database."""
    return cwm_gateway.search_endpoints(q, max_results=max_results)


@router.get("/api-categories", response_model=list[str])
async def api_categories() -> list[str]:
    """List all CW Manage API categories."""
    return cwm_gateway.list_categories()


@router.post("/api-execute", response_model=dict[str, Any] | list[dict[str, Any]])
async def execute_api(req: ApiExecuteRequest) -> Any:
    """Execute an arbitrary CW Manage API call.

    This is a pass-through to the CW Manage API using project credentials.
    Use with caution — this can modify data.
    """
    if req.method.upper() != "GET":
        await enforce_direct_write("connectwise_psa")
    try:
        result = await cwm_gateway.api_request(
            method=req.method,
            endpoint=req.endpoint,
            params=req.params,
            data=req.data,
        )
        log.info("tickets.api_execute", method=req.method, endpoint=req.endpoint)
        return result
    except Exception as exc:
        log.error("tickets.api_execute_error", error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc))
