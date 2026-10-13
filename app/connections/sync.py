"""Local copy: keep what a connection's read operations return, so lookups don't call the API every time.

A **sync** is one read operation of a connection plus its arguments and an interval. Each run:

1. calls the operation through ``service.call_operation`` (same auth, network guard, argument validation and
   read-only rule as any call) as the read-only ``sync`` principal, following the API's paging;
2. finds the list of records in the response (``records_path``, or detected: ``value`` / ``results`` / ``items``
   / ``data`` / ``rows`` / ``documents`` …) and each record's key (``key_field``, or ``id`` / ``key`` / ``uuid`` …,
   or a hash of the record);
3. stores them in ``connection_records``: new ones added, changed ones updated, unchanged ones only touched.
   After a complete run, records the API no longer returned are marked removed (kept for history).

Paging is detected unless ``paging`` says otherwise: a ``Link: rel=next`` header or a next-link field
(``@odata.nextLink``, ``next`` …), a cursor field (``next_cursor``, ``nextPageToken``, GraphQL ``pageInfo`` …)
fed back into the operation's cursor parameter, or a page / offset parameter of the operation. A run stops at an
empty page, a page with nothing new, a short page, or ``max_pages``.

Stored records are searched with the same BM25 engine as operations, plus exact field filters.
Writes never happen here: syncs accept read-only operations only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlparse
from uuid import uuid4

import structlog
from sqlalchemy import delete, func, select, update

from app.auth import audit
from app.auth.principal import READ, ROLE_USER, Principal
from app.connections.base import CallError
from app.connections.search import Index
from app.db.repository import get_session_factory
from app.db.schema import Connection, ConnectionRecord, ConnectionSync, Operation

log = structlog.get_logger(__name__)

MIN_INTERVAL, MAX_INTERVAL = 5, 7 * 24 * 60
MAX_PAGES_LIMIT = 1000
DEFAULT_PAGE_SIZE = 100
CHUNK = 500
PAGING_TYPES = ("auto", "none", "page", "offset", "cursor", "link")


def max_records() -> int:
    return int(os.environ.get("MSP_SYNC_MAX_RECORDS", "200000") or 200000)


def _principal(slug: str, op: str) -> Principal:
    return Principal(id=f"sync:{slug}.{op}", kind="internal", roles=frozenset({ROLE_USER}),
                     scopes=frozenset({"*:read"}), via="sync")


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


# ── Finding records in a response ─────────────────────────────────────────

_LIST_KEYS = ("value", "results", "items", "data", "records", "rows", "documents", "entries", "list", "hits",
              "nodes", "edges", "objects", "d")
_KEY_FIELDS = ("id", "Id", "ID", "uuid", "guid", "key", "_id", "identifier")
_PAGE_PARAMS = ("page", "pageNumber", "page_number", "pageNo", "pageIndex")
_OFFSET_PARAMS = ("offset", "skip", "$skip", "start", "startIndex")
_SIZE_PARAMS = ("pageSize", "page_size", "per_page", "perPage", "pagesize", "limit", "size", "$top", "top")
_CURSOR_PARAMS = ("cursor", "pageToken", "page_token", "after", "continuation", "continuationToken", "next_token",
                  "nextToken", "starting_after", "$skiptoken", "skiptoken")
_CURSOR_FIELDS = ("next_cursor", "nextCursor", "nextPageToken", "next_page_token", "continuationToken",
                  "continuation", "next_token", "nextToken", "endCursor", "cursor", "after")
_LINK_FIELDS = ("@odata.nextLink", "odata.nextLink", "nextLink", "next_link", "next", "next_url", "nextUrl", "__next")
_PAGING_HOLDERS = ("meta", "pagination", "paging", "page_info", "pageInfo", "links", "_links", "cursor", "page")


def get_path(obj: Any, path: str) -> Any:
    """``a.b.0.c`` → obj["a"]["b"][0]["c"] (None when missing)."""
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.lstrip("-").isdigit() and -len(obj) <= int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def _body(result: Any) -> Any:
    """REST / OData / SOAP results wrap the response as ``{"status", "data"}``."""
    if isinstance(result, dict) and "data" in result and set(result) <= {"status", "data", "next_link"}:
        return result["data"]
    return result


def _find_list(node: Any, depth: int = 0) -> list[Any] | None:
    if isinstance(node, list):
        return node
    if not isinstance(node, dict) or depth > 3:
        return None
    for k in _LIST_KEYS:
        v = node.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            found = _find_list(v, depth + 1)
            if found is not None:
                return found
    lists = [v for v in node.values() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)]
    if lists:
        return max(lists, key=len)
    for v in node.values():  # GraphQL: {"tickets": {"nodes": [...]}}
        if isinstance(v, dict):
            found = _find_list(v, depth + 1)
            if found is not None:
                return found
    return None


def extract_records(result: Any, records_path: str = "") -> list[dict[str, Any]]:
    body = _body(result)
    if records_path:
        node = get_path(body, records_path)
        if node is None:
            node = get_path(result, records_path)
        if node is None:
            raise CallError(f"records_path '{records_path}' is not in the response", 422)
        items = node if isinstance(node, list) else [node]
    else:
        found = _find_list(body)
        items = found if found is not None else ([body] if isinstance(body, dict) and body else [])
    out = []
    for item in items:
        if isinstance(item, dict) and set(item) == {"node"} and isinstance(item["node"], dict):
            item = item["node"]  # GraphQL connection edges
        out.append(item if isinstance(item, dict) else {"value": item})
    return out


def _canonical(rec: Any) -> str:
    return json.dumps(rec, sort_keys=True, default=str, separators=(",", ":"))


def record_key(rec: dict[str, Any], key_field: str = "") -> str:
    if key_field:
        v = get_path(rec, key_field)
        if v not in (None, "", [], {}):
            return str(v)[:255]
    else:
        for f in _KEY_FIELDS:
            v = rec.get(f)
            if v not in (None, "", [], {}) and not isinstance(v, dict | list):
                return str(v)[:255]
    return "h:" + hashlib.sha256(_canonical(rec).encode()).hexdigest()[:40]


# ── Paging ────────────────────────────────────────────────────────────────


def _prop_schemas(op: Operation) -> dict[str, Any]:
    props = (op.input_schema or {}).get("properties") if isinstance(op.input_schema, dict) else None
    return props if isinstance(props, dict) else {}


def _props(op: Operation) -> set[str]:
    return set(_prop_schemas(op))


def _coerce(schema: Any, value: str) -> Any:
    """Query-string values from a next-page link are text; give them the type the operation expects."""
    t = schema.get("type") if isinstance(schema, dict) else None
    t = next((x for x in t if x != "null"), None) if isinstance(t, list) else t
    try:
        if t == "integer":
            return int(value)
        if t == "number":
            return float(value)
    except ValueError:
        return value
    if t == "boolean":
        return value.lower() in ("1", "true", "yes")
    return value


def _first(names: tuple[str, ...], props: set[str]) -> str | None:
    lower = {p.lower(): p for p in props}
    for n in names:
        if n in props:
            return n
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def _next_link(result: Any, body: Any) -> str | None:
    if isinstance(result, dict) and isinstance(result.get("next_link"), str):
        return result["next_link"]
    holders = [body] + [body.get(h) for h in _PAGING_HOLDERS if isinstance(body, dict)]
    for h in holders:
        if isinstance(h, dict):
            for f in _LINK_FIELDS:
                v = h.get(f)
                if isinstance(v, dict):
                    v = v.get("href")
                if isinstance(v, str) and (v.startswith(("http://", "https://", "/", "?"))):
                    return v
    return None


def _next_cursor(body: Any, cursor_path: str = "") -> tuple[Any, bool]:
    """(cursor value or None, has_more known False?)."""
    if cursor_path:
        v = get_path(body, cursor_path)
        return (v if v not in ("", [], {}) else None), False
    holders = [body] + [body.get(h) for h in _PAGING_HOLDERS if isinstance(body, dict)]
    if isinstance(body, dict):  # GraphQL: data.<field>.pageInfo
        for v in body.values():
            if isinstance(v, dict) and isinstance(v.get("pageInfo"), dict):
                holders.append(v["pageInfo"])
    for h in holders:
        if not isinstance(h, dict):
            continue
        for flag in ("hasNextPage", "has_more", "hasMore", "more"):
            if h.get(flag) is False:
                return None, True
        for f in _CURSOR_FIELDS:
            v = h.get(f)
            if v not in (None, "", [], {}) and not isinstance(v, dict | list | bool):
                return v, False
    return None, False


class Pager:
    """Works out the arguments for the next page (or None when done)."""

    def __init__(self, op: Operation, args: dict[str, Any], paging: dict[str, Any] | None) -> None:
        self.schemas = _prop_schemas(op)
        self.props = set(self.schemas)
        self.cfg = dict(paging or {})
        self.kind = self.cfg.get("type") or "auto"
        self.args = dict(args)
        self.note = ""
        size_param = self.cfg.get("size_param") or _first(_SIZE_PARAMS[:5], self.props)
        if self.kind != "none" and size_param and size_param not in self.args and size_param in self.props:
            self.args[size_param] = int(self.cfg.get("size") or DEFAULT_PAGE_SIZE)
        self.size = self.args.get(size_param) if size_param else None
        if self.kind == "page":
            param = self.cfg.get("param") or _first(_PAGE_PARAMS, self.props) or "page"
            self.args.setdefault(param, int(self.cfg.get("start", 1)))

    def first(self) -> dict[str, Any]:
        return dict(self.args)

    def next(self, args: dict[str, Any], result: Any, count: int) -> dict[str, Any] | None:
        if self.kind == "none" or count == 0:
            return None
        body = _body(result)
        short = isinstance(self.size, int) and count < self.size
        if self.kind in ("auto", "link"):
            link = _next_link(result, body)
            if link:
                params = dict(parse_qsl(urlparse(link).query, keep_blank_values=True))
                usable = {k: _coerce(self.schemas[k], v) for k, v in params.items() if k in self.props}
                if usable:
                    self.kind = "link"  # the API pages by links: no link on a page means it was the last
                    return {**args, **usable}
                self.note = "The API sent a next-page link this operation has no parameters for; stopped at page 1"
                return None
            if self.kind == "link":
                return None
        if self.kind in ("auto", "cursor"):
            cursor, done = _next_cursor(body, self.cfg.get("cursor_path", ""))
            if done:
                return None
            param = self.cfg.get("param") if self.kind == "cursor" else None
            param = param or _first(_CURSOR_PARAMS, self.props)
            if cursor is not None and param:
                self.kind, self.cfg["param"] = "cursor", param  # likewise for cursors
                return {**args, param: cursor}
            if self.kind == "cursor":
                return None
        if short:
            return None
        if self.kind in ("auto", "page"):
            param = self.cfg.get("param") if self.kind == "page" else None
            param = param or _first(_PAGE_PARAMS, self.props)
            if param:
                current = args.get(param)
                return {**args, param: (int(current) if str(current).lstrip("-").isdigit() else 1) + 1}
        if self.kind in ("auto", "offset"):
            param = self.cfg.get("param") if self.kind == "offset" else None
            param = param or _first(_OFFSET_PARAMS, self.props)
            if param:
                current = args.get(param)
                return {**args, param: (int(current) if str(current).lstrip("-").isdigit() else 0) + count}
        return None


# ── Running a sync ────────────────────────────────────────────────────────

_running: set[str] = set()


async def run_sync(sync_id: str, *, actor: str = "schedule") -> dict[str, Any]:
    """Run one sync now. Returns a summary; errors are stored on the sync, not raised."""
    from app.connections import service

    if sync_id in _running:
        return {"sync": sync_id, "status": "running", "note": "Already running"}
    _running.add(sync_id)
    started = _now()
    summary: dict[str, Any] = {"pages": 0, "fetched": 0, "added": 0, "updated": 0, "unchanged": 0, "removed": 0,
                               "duplicates": 0}
    status, error = "error", ""
    slug = op_name = "?"
    try:
        async with get_session_factory()() as db:
            sync = await db.get(ConnectionSync, sync_id)
            if sync is None:
                raise CallError("No such sync", 404)
            conn = await db.get(Connection, sync.connection_id)
            if conn is None:
                raise CallError("Its connection no longer exists", 404)
            slug, op_name = conn.slug, sync.operation
            op = (await db.execute(select(Operation).where(Operation.connection_id == conn.id,
                                                           Operation.name == sync.operation))).scalars().first()
            sync.status, sync.last_run_at = "running", started
            await db.commit()
            cfg = {"args": dict(sync.args or {}), "records_path": sync.records_path, "key_field": sync.key_field,
                   "paging": sync.paging, "max_pages": max(1, min(sync.max_pages, MAX_PAGES_LIMIT))}
        if op is None:
            raise CallError(f"Operation '{op_name}' no longer exists (re-import removed it?)", 404)
        if not op.read_only:
            raise CallError("Only read operations can be kept locally", 403)

        pager = Pager(op, cfg["args"], cfg["paging"])
        args: dict[str, Any] | None = pager.first()
        records: dict[str, dict[str, Any]] = {}
        complete, seen_pages = True, set()
        limit = max_records()
        while args is not None:
            if summary["pages"] >= cfg["max_pages"]:
                complete = False
                summary["note"] = f"Stopped after max_pages={cfg['max_pages']}; removals not applied"
                break
            page_sig = _canonical(args)
            if page_sig in seen_pages:
                break
            seen_pages.add(page_sig)
            out = await service.call_operation(slug, op_name, args, principal=_principal(slug, op_name),
                                               trim=False, audited=False)
            summary["pages"] += 1
            page = extract_records(out["result"], cfg["records_path"])
            new = 0
            for rec in page:
                key = record_key(rec, cfg["key_field"])
                if key in records:
                    summary["duplicates"] += 1
                else:
                    new += 1
                records[key] = rec
            summary["fetched"] += len(page)
            if len(records) > limit:
                raise CallError(f"More than {limit} records — narrow the arguments or raise MSP_SYNC_MAX_RECORDS",
                                413)
            if page and not new and summary["pages"] > 1:
                break  # the API ignored the page parameter
            args = pager.next(args, out["result"], len(page))
        if pager.note:
            summary["note"] = pager.note
        await _store(sync_id, records, complete, summary)
        status = "ok" if complete else "partial"
    except CallError as exc:
        error = str(exc)
        if getattr(exc, "detail", None):
            error += f" — {json.dumps(exc.detail, default=str)[:300]}"
    except Exception as exc:  # noqa: BLE001 — a broken sync must never take the scheduler down
        log.exception("sync.failed", sync=sync_id)
        error = f"Unexpected error: {exc}"
    finally:
        _running.discard(sync_id)
        summary["seconds"] = round((_now() - started).total_seconds(), 2)
        async with get_session_factory()() as db:
            sync = await db.get(ConnectionSync, sync_id)
            if sync is not None:
                sync.status, sync.last_error, sync.last_summary = status, error[:2000], summary
                if status in ("ok", "partial"):
                    sync.last_ok_at = _now()
                sync.record_count = int((await db.execute(select(func.count()).select_from(ConnectionRecord).where(
                    ConnectionRecord.sync_id == sync_id, ConnectionRecord.removed_at.is_(None)))).scalar() or 0)
                await db.commit()
        await audit.record(principal=f"sync:{slug}.{op_name}", auth_kind="internal", source="sync", method="SYNC",
                           path=f"/api/connections/{slug}/syncs/{sync_id}", action=f"{slug}.{op_name}",
                           status=200 if status in ("ok", "partial") else 500,
                           detail={"by": actor, **summary, **({"error": error[:200]} if error else {})})
        log.info("sync.done", sync=sync_id, connection=slug, operation=op_name, status=status, **summary)
    return {"sync": sync_id, "connection": slug, "operation": op_name, "status": status,
            **({"error": error} if error else {}), **summary}


async def _store(sync_id: str, records: dict[str, dict[str, Any]], complete: bool, summary: dict[str, Any]) -> None:
    now = _now()
    async with get_session_factory()() as db:
        sync = await db.get(ConnectionSync, sync_id)
        existing = {r.record_key: r for r in (await db.execute(
            select(ConnectionRecord.id, ConnectionRecord.record_key, ConnectionRecord.hash,
                   ConnectionRecord.removed_at).where(ConnectionRecord.sync_id == sync_id))).all()}
        new_rows, changed, touched = [], [], []
        for key, rec in records.items():
            digest = hashlib.sha256(_canonical(rec).encode()).hexdigest()
            row = existing.get(key)
            if row is None:
                new_rows.append(ConnectionRecord(id=str(uuid4()), connection_id=sync.connection_id, sync_id=sync_id,
                                                 record_key=key, data=rec, hash=digest, first_seen=now,
                                                 last_seen=now, changed_at=now))
            elif row.hash != digest or row.removed_at is not None:
                changed.append({"id": row.id, "data": rec, "hash": digest, "last_seen": now, "changed_at": now,
                                "removed_at": None})
            else:
                touched.append(row.id)
        db.add_all(new_rows)
        if changed:
            await db.execute(update(ConnectionRecord), changed)
        for i in range(0, len(touched), CHUNK):
            await db.execute(update(ConnectionRecord).where(ConnectionRecord.id.in_(touched[i:i + CHUNK]))
                             .values(last_seen=now), execution_options={"synchronize_session": False})
        gone = [r.id for k, r in existing.items() if k not in records and r.removed_at is None] if complete else []
        for i in range(0, len(gone), CHUNK):
            await db.execute(update(ConnectionRecord).where(ConnectionRecord.id.in_(gone[i:i + CHUNK]))
                             .values(removed_at=now, changed_at=now), execution_options={"synchronize_session": False})
        await db.commit()
    summary.update(added=len(new_rows), updated=len(changed), unchanged=len(touched), removed=len(gone),
                   records=len(records))


# ── Managing syncs ────────────────────────────────────────────────────────


def sync_summary(s: ConnectionSync, operation_title: str = "") -> dict[str, Any]:
    nxt = (_aware(s.last_run_at) + timedelta(minutes=s.interval_minutes)) if s.last_run_at else None
    return {"id": s.id, "operation": s.operation, "title": operation_title, "args": s.args or {},
            "interval_minutes": s.interval_minutes, "records_path": s.records_path, "key_field": s.key_field,
            "paging": s.paging, "max_pages": s.max_pages, "enabled": s.enabled, "status": s.status,
            "last_error": s.last_error or None, "last_summary": s.last_summary, "record_count": s.record_count,
            "last_run_at": s.last_run_at, "last_ok_at": s.last_ok_at,
            "next_run_at": nxt if s.enabled else None, "created_by": s.created_by}


def _clean(body: dict[str, Any], op: Operation | None, partial: bool) -> dict[str, Any]:
    from app.connections.service import validate_args

    out: dict[str, Any] = {}
    if "args" in body or not partial:
        args = body.get("args") or {}
        if not isinstance(args, dict):
            raise CallError("args must be an object", 400)
        if op is not None:
            validate_args(op.input_schema, args)
        out["args"] = args
    if "interval_minutes" in body or not partial:
        try:
            minutes = int(body.get("interval_minutes") or 60)
        except (TypeError, ValueError) as exc:
            raise CallError("interval_minutes must be a number", 400) from exc
        out["interval_minutes"] = max(MIN_INTERVAL, min(minutes, MAX_INTERVAL))
    for f in ("records_path", "key_field"):
        if f in body or not partial:
            v = str(body.get(f) or "").strip()
            if len(v) > 255 or re.search(r"[\x00-\x1f]", v):
                raise CallError(f"{f} is too long or has control characters", 400)
            out[f] = v
    if "paging" in body or not partial:
        paging = body.get("paging") or None
        if paging is not None:
            if not isinstance(paging, dict) or paging.get("type", "auto") not in PAGING_TYPES:
                raise CallError(f"paging.type must be one of: {', '.join(PAGING_TYPES)}", 400)
            paging = {k: v for k, v in paging.items()
                      if k in ("type", "param", "start", "size", "size_param", "cursor_path")}
        out["paging"] = paging
    if "max_pages" in body or not partial:
        try:
            out["max_pages"] = max(1, min(int(body.get("max_pages") or 50), MAX_PAGES_LIMIT))
        except (TypeError, ValueError) as exc:
            raise CallError("max_pages must be a number", 400) from exc
    if "enabled" in body:
        out["enabled"] = bool(body["enabled"])
    return out


async def _conn(db: Any, slug: str) -> Connection:
    conn = (await db.execute(select(Connection).where(Connection.slug == slug))).scalar_one_or_none()
    if conn is None:
        raise CallError(f"No connection '{slug}'", 404)
    return conn


async def list_syncs(slug: str) -> list[dict[str, Any]]:
    async with get_session_factory()() as db:
        conn = await _conn(db, slug)
        syncs = (await db.execute(select(ConnectionSync).where(ConnectionSync.connection_id == conn.id)
                                  .order_by(ConnectionSync.operation))).scalars().all()
        titles = dict((await db.execute(select(Operation.name, Operation.title).where(
            Operation.connection_id == conn.id, Operation.name.in_([s.operation for s in syncs] or [""])))).all())
    return [sync_summary(s, titles.get(s.operation, "")) for s in syncs]


async def create_sync(slug: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
    name = str(body.get("operation") or "").strip()
    async with get_session_factory()() as db:
        conn = await _conn(db, slug)
        op = (await db.execute(select(Operation).where(Operation.connection_id == conn.id,
                                                       Operation.name == name))).scalars().first()
        if op is None:
            raise CallError(f"No operation '{name}' in '{slug}'", 404)
        if not op.read_only:
            raise CallError("Only read operations can be kept locally", 400)
        if (await db.execute(select(ConnectionSync).where(ConnectionSync.connection_id == conn.id,
                                                          ConnectionSync.operation == name))).scalars().first():
            raise CallError(f"'{name}' already has a local copy — change it instead", 409)
        fields = _clean(body, op, partial=False)
        s = ConnectionSync(id=str(uuid4()), connection_id=conn.id, operation=name, enabled=True, created_by=actor,
                           created_at=_now(), **fields)
        db.add(s)
        await db.commit()
    log.info("sync.created", connection=slug, operation=name, by=actor)
    return sync_summary(s, op.title)


async def _get_sync(db: Any, slug: str, sync_id: str) -> tuple[Connection, ConnectionSync]:
    conn = await _conn(db, slug)
    s = await db.get(ConnectionSync, sync_id)
    if s is None or s.connection_id != conn.id:
        raise CallError("No such sync on this connection", 404)
    return conn, s


async def update_sync(slug: str, sync_id: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
    async with get_session_factory()() as db:
        conn, s = await _get_sync(db, slug, sync_id)
        op = (await db.execute(select(Operation).where(Operation.connection_id == conn.id,
                                                       Operation.name == s.operation))).scalars().first()
        for k, v in _clean(body, op, partial=True).items():
            setattr(s, k, v)
        await db.commit()
        log.info("sync.updated", connection=slug, operation=s.operation, by=actor, fields=sorted(body))
        return sync_summary(s, op.title if op else "")


async def delete_sync(slug: str, sync_id: str, actor: str) -> dict[str, Any]:
    async with get_session_factory()() as db:
        _, s = await _get_sync(db, slug, sync_id)
        removed = (await db.execute(delete(ConnectionRecord).where(ConnectionRecord.sync_id == sync_id),
                                    execution_options={"synchronize_session": False})).rowcount
        await db.delete(s)
        await db.commit()
    log.info("sync.deleted", connection=slug, sync=sync_id, records=removed, by=actor)
    return {"id": sync_id, "deleted": True, "records_deleted": removed or 0}


async def sync_belongs(slug: str, sync_id: str) -> None:
    async with get_session_factory()() as db:
        await _get_sync(db, slug, sync_id)


# ── Scheduler ─────────────────────────────────────────────────────────────


async def run_due(now: datetime | None = None) -> list[dict[str, Any]]:
    """Run every enabled sync whose interval has passed (one at a time, to be gentle on the APIs)."""
    now = now or _now()
    async with get_session_factory()() as db:
        rows = (await db.execute(select(ConnectionSync, Connection.enabled).join(
            Connection, Connection.id == ConnectionSync.connection_id).where(ConnectionSync.enabled.is_(True)))).all()
    due = [s.id for s, conn_enabled in rows if conn_enabled and (
        s.last_run_at is None or _aware(s.last_run_at) + timedelta(minutes=s.interval_minutes) <= now)]
    return [await run_sync(sid) for sid in due]


def start_sync_loop() -> asyncio.Task[Any] | None:
    if os.environ.get("MSP_CONNECTION_SYNC", "on").lower() in ("0", "off", "false", "no"):
        log.info("sync.loop_disabled")
        return None
    return asyncio.create_task(_loop())


async def _loop() -> None:
    await asyncio.sleep(30)  # let startup finish
    while True:
        try:
            await run_due()
        except Exception:  # noqa: BLE001
            log.exception("sync.loop_error")
        await asyncio.sleep(60)


# ── Searching the local copy ──────────────────────────────────────────────

_INDEX_CACHE: OrderedDict[str, tuple[Any, Index]] = OrderedDict()
_INDEX_KEEP = 6
_TEXT_MAX = 2000


def _flatten(obj: Any, out: list[str], depth: int = 0) -> None:
    if sum(len(x) for x in out) > _TEXT_MAX or depth > 6:
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _flatten(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:50]:
            _flatten(v, out, depth + 1)
    elif isinstance(obj, str | int | float) and not isinstance(obj, bool):
        out.append(str(obj)[:300])


async def _records_index(conn: Connection) -> Index:
    async with get_session_factory()() as db:
        version = (await db.execute(select(func.count(), func.max(ConnectionRecord.changed_at),
                                           func.max(ConnectionRecord.last_seen)).where(
            ConnectionRecord.connection_id == conn.id))).one()
    cached = _INDEX_CACHE.get(conn.id)
    if cached and cached[0] == tuple(version):
        _INDEX_CACHE.move_to_end(conn.id)
        return cached[1]
    async with get_session_factory()() as db:
        ops = dict((await db.execute(select(ConnectionSync.id, ConnectionSync.operation).where(
            ConnectionSync.connection_id == conn.id))).all())
        rows = (await db.execute(select(ConnectionRecord).where(ConnectionRecord.connection_id == conn.id)
                                 .order_by(ConnectionRecord.record_key))).scalars().all()
    docs = []
    for r in rows:
        words: list[str] = []
        _flatten(r.data, words)
        docs.append({"name": r.record_key, "text": " ".join(words), "operation": ops.get(r.sync_id, "?"),
                     "connection": conn.slug, "row": r})
    index = Index(docs, fields={"key": ["name"], "text": ["text"]}, weights={"key": 3.0, "text": 1.0})
    _INDEX_CACHE[conn.id] = (tuple(version), index)
    while len(_INDEX_CACHE) > _INDEX_KEEP:
        _INDEX_CACHE.popitem(last=False)
    return index


def _matches(data: Any, filters: dict[str, Any]) -> bool:
    for path, want in filters.items():
        have = get_path(data, path)
        wants = want if isinstance(want, list) else [want]
        haves = have if isinstance(have, list) else [have]
        norm = {str(w).lower() for w in wants}
        if not any(str(h).lower() in norm for h in haves if h is not None):
            return False
    return True


def _out(doc: dict[str, Any], score: float | None = None) -> dict[str, Any]:
    r: ConnectionRecord = doc["row"]
    return {"connection": doc["connection"], "operation": doc["operation"], "key": r.record_key,
            **({"score": "exact key" if score >= 1e9 else score} if score is not None else {}), "data": r.data, "last_seen": r.last_seen,
            "changed_at": r.changed_at, **({"removed_at": r.removed_at} if r.removed_at else {})}


async def search_records(connections: list[Connection], q: str = "", *, filters: dict[str, Any] | None = None,
                         operation: str | None = None, include_removed: bool = False, limit: int = 20,
                         offset: int = 0) -> dict[str, Any]:
    """Ranked search (``q``) and/or exact field filters (``{"status": "open", "company.id": 42}``) over the
    local copy of the given connections."""
    filters = filters or {}
    limit = max(1, min(limit, 200))

    def keep(doc: dict[str, Any]) -> bool:
        r: ConnectionRecord = doc["row"]
        if r.removed_at is not None and not include_removed:
            return False
        if operation and doc["operation"] != operation:
            return False
        return _matches(r.data, filters) if filters else True

    hits: list[tuple[float | None, dict[str, Any]]] = []
    wanted = q.strip()
    for conn in connections:
        index = await _records_index(conn)
        if wanted:
            # an exact key ("4", "INV-1001") always comes first, even when it is too short for word search
            exact = [d.record for d in index.docs if d.record["name"] == wanted and keep(d.record)]
            hits += [(1e9, doc) for doc in exact]
            hits += [(score, doc) for score, doc, _ in index.search(q, limit=offset + limit, predicate=keep)
                     if doc["name"] != wanted]
        else:
            hits += [(None, d.record) for d in index.docs if keep(d.record)]
    if wanted:
        hits.sort(key=lambda h: -(h[0] or 0))
    total = len(hits)
    page = hits[offset:offset + limit]
    return {"query": q, "filters": filters, "total": total if not q.strip() else None,
            "count": len(page), "records": [_out(d, s) for s, d in page]}


async def get_record(conn: Connection, key: str, operation: str | None = None) -> dict[str, Any] | None:
    async with get_session_factory()() as db:
        stmt = select(ConnectionRecord).where(ConnectionRecord.connection_id == conn.id,
                                              ConnectionRecord.record_key == key)
        rows = (await db.execute(stmt.order_by(ConnectionRecord.removed_at.is_not(None)))).scalars().all()
        ops = dict((await db.execute(select(ConnectionSync.id, ConnectionSync.operation).where(
            ConnectionSync.connection_id == conn.id))).all())
    for r in rows:
        if operation and ops.get(r.sync_id) != operation:
            continue
        return _out({"row": r, "connection": conn.slug, "operation": ops.get(r.sync_id, "?")})
    return None


async def connections_with_records(principal: Principal, slug: str | None = None) -> list[Connection]:
    """Enabled connections the caller may read that have at least one sync (optionally just ``slug``)."""
    async with get_session_factory()() as db:
        stmt = select(Connection).where(Connection.enabled.is_(True), Connection.id.in_(
            select(ConnectionSync.connection_id)))
        if slug:
            stmt = stmt.where(Connection.slug == slug)
        rows = (await db.execute(stmt.order_by(Connection.name))).scalars().all()
    return [c for c in rows if principal.allows(c.slug, READ)]


async def freshness(conn: Connection) -> list[dict[str, Any]]:
    """What is kept locally for a connection and how fresh it is (for MCP instructions and tools)."""
    async with get_session_factory()() as db:
        syncs = (await db.execute(select(ConnectionSync).where(ConnectionSync.connection_id == conn.id)
                                  .order_by(ConnectionSync.operation))).scalars().all()
    return [{"operation": s.operation, "records": s.record_count, "every_minutes": s.interval_minutes,
             "last_synced": s.last_ok_at, "status": s.status} for s in syncs]
