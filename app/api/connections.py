"""Connections API — import any API / data source and call its operations.

Managing connections (create, re-import, edit, delete, operation flags) needs the Approver role (middleware).
Listing, searching and calling are open to every signed-in caller; calls are checked per operation
(read scope, write mode, write scope) in ``app/connections/service.py``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth.principal import get_current
from app.connections import service, sync
from app.connections.adapters import KINDS
from app.connections.base import CallError, SourceError, UpstreamError
from app.connections.http import AUTH_TYPES

router = APIRouter(prefix="/api/connections", tags=["connections"])
search_router = APIRouter(prefix="/api/search", tags=["connections"])


@search_router.get("/operations")
async def search_operations(q: str = Query(..., min_length=1, max_length=300), connection: str | None = None,
                            kind: str | None = None, read_only: bool | None = None, pinned: bool | None = None,
                            limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    """Ranked search across every connection the caller may use (``connection`` = one slug or a comma list)."""
    from app.auth.principal import READ
    from app.connections.search import search_operations as search

    p = get_current()
    hits = await search(q, connection=connection, kind=kind, read_only=read_only, pinned=pinned, limit=limit,
                        allowed=(lambda slug: p.allows(slug, READ)) if p else (lambda slug: False))
    return {"query": q, "count": len(hits), "results": hits}


def _actor() -> str:
    p = get_current()
    return p.id if p else "unknown"


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, UpstreamError):
        return JSONResponse({"detail": str(exc), "upstream_status": exc.status, "upstream": exc.detail}, 502)
    if isinstance(exc, CallError):
        return JSONResponse({"detail": str(exc), **({"errors": exc.detail} if exc.detail else {})}, exc.status)
    return JSONResponse({"detail": str(exc)}, 400)


class ConnectionBody(BaseModel):
    kind: str
    name: str = ""
    slug: str = ""
    base_url: str = ""
    source: dict[str, Any] = {}
    auth: dict[str, Any] = {}
    settings: dict[str, Any] = {}


class CallBody(BaseModel):
    args: dict[str, Any] = {}


@router.get("/kinds")
async def kinds() -> dict[str, Any]:
    """What can be connected, and the auth types (with their secret / setting fields)."""
    return {"kinds": list(KINDS), "auth_types": AUTH_TYPES}


@router.get("")
async def list_connections() -> list[dict[str, Any]]:
    return await service.list_connections()


@router.post("")
async def create_connection(body: ConnectionBody, dry_run: bool = False) -> Any:
    """Import a definition and create the connection. ``?dry_run=true`` previews without saving."""
    try:
        return await service.create_connection(body.model_dump(), _actor(), dry_run=dry_run)
    except (SourceError, CallError, ValueError) as exc:
        return _error(exc)


# ── Templates and the public API directory (declared before /{slug}) ─────────────────────────────────────


@router.get("/templates")
async def list_templates(q: str = "", category: str = "") -> dict[str, Any]:
    """Well-known APIs that connect with only your credentials (app/connection_templates/templates.yaml)."""
    from app.services import connection_templates as ct

    return {"categories": ct.CATEGORIES, "templates": ct.search(q, category)}


class FromTemplateBody(BaseModel):
    template: str
    name: str = ""
    values: dict[str, Any] = {}
    auth: dict[str, Any] | None = None  # only for templates whose sign-in is read from the definition


@router.post("/from-template")
async def create_from_template(body: FromTemplateBody, dry_run: bool = False) -> Any:
    """Preview (``dry_run``) or save a connection from a template; saving pins the template's suggested tools."""
    from app.services import connection_templates as ct

    try:
        conn_body = ct.build_body(body.template, body.values, body.name, body.auth)
        if not dry_run:
            conn_body["slug"] = await service.free_slug(conn_body.get("slug") or service.slugify(conn_body["name"]))
        result = await service.create_connection(conn_body, _actor(), dry_run=dry_run)
        if not dry_run:
            await service.flag_by_path(result["slug"], ct.read_only(body.template), "read_only", _actor())
            result["pinned"] = await service.pin_by_path(result["slug"], ct.suggested(body.template), _actor())
        return result
    except (ct.TemplateError, SourceError, CallError, ValueError) as exc:
        return _error(exc)


@router.get("/directory")
async def search_directory(q: str = "", limit: int = Query(30, ge=1, le=100)) -> Any:
    """Search ~2,500 public APIs (APIs.guru) — for anything without a template."""
    from app.services import api_directory

    try:
        return {"query": q, "results": await api_directory.search(q, limit)}
    except api_directory.DirectoryUnavailable as exc:
        return JSONResponse({"detail": str(exc)}, 503)


@router.get("/{slug}")
async def get_connection(slug: str) -> Any:
    try:
        return await service.get_connection(slug)
    except CallError as exc:
        return _error(exc)


@router.patch("/{slug}")
async def update_connection(slug: str, body: dict[str, Any]) -> Any:
    try:
        return await service.update_connection(slug, body, _actor())
    except (SourceError, CallError) as exc:
        return _error(exc)


@router.delete("/{slug}")
async def delete_connection(slug: str) -> Any:
    try:
        return await service.delete_connection(slug, _actor())
    except CallError as exc:
        return _error(exc)


@router.post("/{slug}/reimport")
async def reimport(slug: str, body: dict[str, Any] | None = None) -> Any:
    """Re-read the definition (optionally a new ``source``). Pinned / enabled / read-only choices are kept."""
    try:
        return await service.reimport(slug, _actor(), (body or {}).get("source"))
    except (SourceError, CallError) as exc:
        return _error(exc)


@router.get("/{slug}/operations")
async def list_operations(slug: str, q: str = "", limit: int = Query(100, ge=1, le=500), offset: int = 0,
                          read_only: bool | None = None, pinned: bool | None = None) -> Any:
    try:
        return await service.list_operations(slug, q, limit, offset, read_only, pinned)
    except CallError as exc:
        return _error(exc)


@router.get("/{slug}/operations/{name}")
async def get_operation(slug: str, name: str) -> Any:
    try:
        return await service.get_operation(slug, name)
    except CallError as exc:
        return _error(exc)


@router.patch("/{slug}/operations/{name}")
async def update_operation(slug: str, name: str, body: dict[str, Any]) -> Any:
    """Pin / unpin, enable / disable, or override read-only for one operation."""
    try:
        return await service.update_operation(slug, name, body, _actor())
    except CallError as exc:
        return _error(exc)


@router.post("/{slug}/operations/{name}/call")
async def call_operation(slug: str, name: str, body: CallBody) -> Any:
    try:
        out = await service.call_operation(slug, name, body.args)
    except (SourceError, CallError) as exc:
        return _error(exc)
    return JSONResponse(jsonable_encoder(out), 202) if out.get("pending_approval") else out


# ── Local copy (syncs + records) ──────────────────────────────────────────
# Managing syncs needs the Approver role (middleware). Reading them and the stored records needs read access
# to the connection (scope), checked here because stored records are data.


def _readable(slug: str) -> JSONResponse | None:
    from app.auth.principal import READ

    p = get_current()
    if p is None or not p.allows(slug, READ):
        return JSONResponse({"detail": f"Your credential may not use '{slug}'"}, 403)
    return None


def _filters(request: Request) -> dict[str, Any]:
    """``?f.status=open&f.company.id=42`` → {"status": "open", "company.id": "42"} (repeat a key for any-of)."""
    out: dict[str, Any] = {}
    for key in {k for k in request.query_params if k.startswith("f.") and len(k) > 2}:
        values = request.query_params.getlist(key)
        out[key[2:]] = values if len(values) > 1 else values[0]
    return out


@router.get("/{slug}/syncs")
async def list_syncs(slug: str) -> Any:
    if denied := _readable(slug):
        return denied
    try:
        return await sync.list_syncs(slug)
    except CallError as exc:
        return _error(exc)


@router.post("/{slug}/syncs")
async def create_sync(slug: str, body: dict[str, Any], run: bool = True) -> Any:
    """Keep a local copy of a read operation: ``{"operation", "args", "interval_minutes", "records_path",
    "key_field", "paging", "max_pages"}``. Runs once right away unless ``?run=false``."""
    try:
        created = await sync.create_sync(slug, body, _actor())
    except CallError as exc:
        return _error(exc)
    if run:
        _start(created["id"])
        created["status"] = "running"
    return created


@router.patch("/{slug}/syncs/{sync_id}")
async def update_sync(slug: str, sync_id: str, body: dict[str, Any]) -> Any:
    try:
        return await sync.update_sync(slug, sync_id, body, _actor())
    except CallError as exc:
        return _error(exc)


@router.delete("/{slug}/syncs/{sync_id}")
async def delete_sync(slug: str, sync_id: str) -> Any:
    """Stop keeping a local copy and delete the stored records."""
    try:
        return await sync.delete_sync(slug, sync_id, _actor())
    except CallError as exc:
        return _error(exc)


@router.post("/{slug}/syncs/{sync_id}/run")
async def run_sync(slug: str, sync_id: str, wait: bool = False) -> Any:
    """Sync now. ``?wait=true`` waits for the result; otherwise it runs in the background."""
    try:
        await sync.sync_belongs(slug, sync_id)
    except CallError as exc:
        return _error(exc)
    if wait:
        return jsonable_encoder(await sync.run_sync(sync_id, actor=_actor()))
    _start(sync_id)
    return JSONResponse({"sync": sync_id, "status": "running"}, 202)


_tasks: set[Any] = set()


def _start(sync_id: str) -> None:
    import asyncio

    task = asyncio.create_task(sync.run_sync(sync_id, actor=_actor()))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.get("/{slug}/records")
async def list_records(slug: str, request: Request, q: str = Query("", max_length=300), operation: str | None = None,
                       include_removed: bool = False, limit: int = Query(20, ge=1, le=200),
                       offset: int = Query(0, ge=0)) -> Any:
    """Search the local copy: ``q`` ranks by words; ``f.<field>=<value>`` filters exactly (dotted paths)."""
    if denied := _readable(slug):
        return denied
    conns = await sync.connections_with_records(get_current(), slug)
    if not conns:
        return JSONResponse({"detail": f"'{slug}' keeps no local copy (or is disabled)"}, 404)
    return jsonable_encoder(await sync.search_records(conns, q, filters=_filters(request), operation=operation,
                                                      include_removed=include_removed, limit=limit, offset=offset))


@router.get("/{slug}/records/{key}")
async def get_record(slug: str, key: str, operation: str | None = None) -> Any:
    if denied := _readable(slug):
        return denied
    conns = await sync.connections_with_records(get_current(), slug)
    found = await sync.get_record(conns[0], key, operation) if conns else None
    if found is None:
        return JSONResponse({"detail": f"No stored record '{key}' in '{slug}'"}, 404)
    return jsonable_encoder(found)


@search_router.get("/records")
async def search_records(request: Request, q: str = Query("", max_length=300), connection: str | None = None,
                         operation: str | None = None, include_removed: bool = False,
                         limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0)) -> Any:
    """Search the local copy of every connection the caller may use (or one: ``connection``)."""
    p = get_current()
    conns = await sync.connections_with_records(p, connection) if p else []
    return jsonable_encoder(await sync.search_records(conns, q, filters=_filters(request), operation=operation,
                                                      include_removed=include_removed, limit=limit, offset=offset))
