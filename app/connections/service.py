"""Connections: import, store, list, search and call — with validation, permissions, write modes and audit."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from sqlalchemy import delete, func, or_, select

from app.auth import audit
from app.auth.crypto import master_key_configured
from app.auth.principal import READ, Principal, get_current
from app.connections.adapters import KINDS, get_adapter
from app.connections.base import CallError, ImportResult, OperationDef, SourceError, UpstreamError
from app.connections.http import AUTH_TYPES, ConnCtx
from app.db.repository import get_session_factory
from app.db.schema import Connection, ConnectionRecord, ConnectionSync, Operation

log = structlog.get_logger(__name__)

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
# Names already used by built-in platform connectors / reserved routes.
RESERVED_SLUGS = {"connectwise_psa", "sentinelone", "eset", "eset_connect", "asio", "screenconnect", "avast",
                  "generic_rest", "msp_api_tools", "msp", "admin", "api", "mcp", "all"}
MAX_STORED_SOURCE = 10 * 1024 * 1024
MAX_RESULT_CHARS = 200_000
CALL_TIMEOUT_S = 120


def _now() -> datetime:
    return datetime.now(UTC)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not s or not s[0].isalpha():
        s = f"api_{s}"
    return s[:40]


def ctx_of(c: Connection) -> ConnCtx:
    return ConnCtx(id=c.id, slug=c.slug, kind=c.kind, base_url=c.base_url or "", auth_type=c.auth_type or "none",
                   secrets=dict(c.secrets or {}), settings=dict(c.settings or {}), source=dict(c.source or {}))


def summary(c: Connection) -> dict[str, Any]:
    return {"slug": c.slug, "name": c.name, "kind": c.kind, "base_url": c.base_url, "auth_type": c.auth_type,
            "enabled": c.enabled, "operations": c.operation_count, "description": c.description,
            "settings": c.settings or {}, "source": {k: v for k, v in (c.source or {}).items() if k != "text"},
            "secrets_set": sorted((c.secrets or {}).keys()), "created_by": c.created_by,
            "created_at": c.created_at, "imported_at": c.imported_at}


def op_summary(o: Operation, full: bool = False) -> dict[str, Any]:
    out = {"name": o.name, "title": o.title, "method": o.method, "target": o.target, "read_only": o.read_only,
           "pinned": o.pinned, "enabled": o.enabled, "deprecated": o.deprecated, "tags": o.tags or []}
    if full:
        out.update({"key": o.key, "description": o.description, "input_schema": o.input_schema,
                    "output_schema": o.output_schema})
    else:
        out["description"] = (o.description or "")[:300]
    return out


# ── Import ────────────────────────────────────────────────────────────────


def _split_auth(auth: dict[str, Any] | None) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """{"type": "api_key", "api_key": "…", "api_key_name": "X-Key"} → (type, secrets, settings)."""
    auth = dict(auth or {})
    t = str(auth.pop("type", "") or auth.pop("auth_type", "") or "none")
    if t not in AUTH_TYPES:
        raise SourceError(f"Unknown auth type '{t}'. Use one of: {', '.join(AUTH_TYPES)}")
    spec = AUTH_TYPES[t]
    secrets = {k: auth[k] for k in spec["secrets"] if auth.get(k) not in (None, "")}
    if isinstance(auth.get("variables"), dict):
        secrets["variables"] = auth["variables"]
    if auth.get("dsn"):
        secrets["dsn"] = auth["dsn"]
    settings = {k: auth[k] for k in spec["settings"] if auth.get(k) not in (None, "")}
    return t, secrets, settings


def _stored_source(source: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in source.items() if k in ("url", "text", "use_auth")}
    if "text" in out and len(str(out["text"]).encode()) > MAX_STORED_SOURCE:
        out.pop("text")
        out["note"] = "Pasted definition was too large to keep; paste it again to re-import"
    return out


def _hint_public(hint: dict[str, Any] | None) -> dict[str, Any] | None:
    if not hint:
        return None
    out = {k: v for k, v in hint.items() if k != "secrets"}
    if hint.get("secrets"):
        out["credentials_found"] = sorted(hint["secrets"].keys())
    return out


async def _discover(ctx: ConnCtx, source: dict[str, Any]) -> ImportResult:
    adapter = get_adapter(ctx.kind)
    try:
        return await asyncio.wait_for(adapter.discover(ctx, source), timeout=300)
    except asyncio.TimeoutError as exc:
        raise CallError("Reading the definition took longer than 5 minutes", 504) from exc


def _rows(connection_id: str, ops: list[OperationDef], keep: dict[str, Operation] | None = None) -> list[Operation]:
    rows = []
    for op in ops:
        prev = (keep or {}).get(op.key)
        rows.append(Operation(
            id=str(uuid4()), connection_id=connection_id, key=op.key[:255], name=op.name[:128], title=op.title[:512],
            description=op.description, tags=op.tags, method=op.method[:16], target=op.target,
            input_schema=op.input_schema, output_schema=op.output_schema, binding=op.binding,
            read_only=prev.read_only if prev else op.read_only, pinned=prev.pinned if prev else False,
            enabled=prev.enabled if prev else True, deprecated=op.deprecated, search_text=op.search_text()))
    return rows


async def create_connection(body: dict[str, Any], actor: str, dry_run: bool = False) -> dict[str, Any]:
    kind = str(body.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise SourceError(f"kind must be one of: {', '.join(KINDS)}")
    name = str(body.get("name") or "").strip()[:255]
    slug = str(body.get("slug") or "").strip().lower() or (slugify(name) if name else "")
    source = dict(body.get("source") or {})
    auth_type, secrets, auth_settings = _split_auth(body.get("auth"))
    settings = {**dict(body.get("settings") or {}), **auth_settings}

    ctx = ConnCtx(id=str(uuid4()), slug=slug or "draft", kind=kind, base_url=str(body.get("base_url") or "").strip(),
                  auth_type=auth_type, secrets=secrets, settings=settings, source=source)
    result = await _discover(ctx, source)

    hint = result.auth_hint or {}
    used_hint_secrets = False
    if auth_type == "none" and hint.get("auth_type") and hint["auth_type"] != "none":
        auth_type = hint["auth_type"]
        settings.update({k: hint[k] for k in AUTH_TYPES[auth_type]["settings"] if hint.get(k)})
    if not secrets and hint.get("secrets"):
        secrets = {k: v for k, v in hint["secrets"].items() if v not in (None, "", {})}
        used_hint_secrets = bool(secrets)
    settings = {**result.settings, **settings}
    base_url = ctx.base_url or result.base_url
    name = name or result.name or slug or kind
    slug = slug or slugify(name)
    warnings = list(result.warnings)
    if used_hint_secrets:
        warnings.append("Credentials found in the definition were stored (encrypted) as this connection's auth")
    if not base_url and kind not in ("sql", "mongodb"):
        warnings.append("No base URL found — set one before calling operations")

    preview = {"slug": slug, "name": name, "kind": kind, "base_url": base_url, "auth_type": auth_type,
               "auth_hint": _hint_public(result.auth_hint), "operations": len(result.operations),
               "read_operations": sum(1 for o in result.operations if o.read_only),
               "write_operations": sum(1 for o in result.operations if not o.read_only),
               "sample": [{"name": o.name, "title": o.title, "method": o.method, "read_only": o.read_only}
                          for o in result.operations[:50]], "warnings": warnings}
    if dry_run:
        return {"dry_run": True, **preview}

    if not SLUG_RE.match(slug) or slug in RESERVED_SLUGS:
        raise SourceError(f"Invalid or reserved slug '{slug}' — 2–41 chars, a-z, 0-9, _ ; starts with a letter")
    if secrets and not master_key_configured():
        raise CallError("Saving credentials needs MSP_MASTER_KEY in .env (python -m app.cli gen-master-key)", 503)
    async with get_session_factory()() as db:
        if (await db.execute(select(Connection).where(Connection.slug == slug))).scalar_one_or_none():
            raise SourceError(f"A connection called '{slug}' already exists")
        conn = Connection(id=ctx.id, slug=slug, name=name, kind=kind, base_url=base_url, auth_type=auth_type,
                          secrets=secrets or None, settings=settings, source=_stored_source(source),
                          description=result.description, enabled=True, operation_count=len(result.operations),
                          created_by=actor, created_at=_now(), imported_at=_now())
        db.add(conn)
        db.add_all(_rows(conn.id, result.operations))
        await db.commit()
    log.info("connection.created", slug=slug, kind=kind, operations=len(result.operations), by=actor)
    return {"created": True, **preview}


async def _get(db: Any, slug: str) -> Connection:
    conn = (await db.execute(select(Connection).where(Connection.slug == slug))).scalar_one_or_none()
    if conn is None:
        raise CallError(f"No connection '{slug}'", 404)
    return conn


async def reimport(slug: str, actor: str, source: dict[str, Any] | None = None) -> dict[str, Any]:
    async with get_session_factory()() as db:
        conn = await _get(db, slug)
        src = dict(source or conn.source or {})
        if not (src.get("url") or src.get("text")) and conn.kind not in ("sql", "mongodb", "mcp", "graphql", "odata"):
            raise SourceError("No stored definition to re-import — pass a new source")
        _forget(conn)
        result = await _discover(ctx_of(conn), src)
        existing = {o.key: o for o in (await db.execute(select(Operation).where(Operation.connection_id == conn.id))).scalars()}
        await db.execute(delete(Operation).where(Operation.connection_id == conn.id),
                         execution_options={"synchronize_session": False})
        db.add_all(_rows(conn.id, result.operations, existing))
        conn.operation_count = len(result.operations)
        conn.imported_at = _now()
        if source:
            conn.source = _stored_source(source)
        await db.commit()
    added = len({o.key for o in result.operations} - set(existing))
    removed = len(set(existing) - {o.key for o in result.operations})
    log.info("connection.reimported", slug=slug, operations=len(result.operations), by=actor)
    return {"slug": slug, "operations": len(result.operations), "added": added, "removed": removed,
            "warnings": result.warnings}


def _forget(conn: Connection) -> None:
    if conn.kind == "soap":
        from app.connections.adapters import soap
        soap.forget(conn.id)
    elif conn.kind == "mcp":
        from app.connections.adapters import mcp
        mcp.forget(conn.id)


async def update_connection(slug: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
    async with get_session_factory()() as db:
        conn = await _get(db, slug)
        if "name" in body:
            conn.name = str(body["name"]).strip()[:255] or conn.name
        if "base_url" in body:
            conn.base_url = str(body["base_url"] or "").strip()
        if "enabled" in body:
            conn.enabled = bool(body["enabled"])
        if "settings" in body and isinstance(body["settings"], dict):
            conn.settings = {**(conn.settings or {}), **body["settings"]}
        if "auth" in body:
            auth_type, secrets, auth_settings = _split_auth(body["auth"])
            if secrets and not master_key_configured():
                raise CallError("Saving credentials needs MSP_MASTER_KEY", 503)
            same_type = auth_type == conn.auth_type
            conn.auth_type = auth_type
            conn.secrets = {**(conn.secrets or {}), **secrets} if same_type else secrets
            conn.settings = {**(conn.settings or {}), **auth_settings}
        _forget(conn)
        await db.commit()
        log.info("connection.updated", slug=slug, by=actor, fields=sorted(body))
        return summary(conn)


async def delete_connection(slug: str, actor: str) -> dict[str, Any]:
    async with get_session_factory()() as db:
        conn = await _get(db, slug)
        _forget(conn)
        for table in (Operation, ConnectionSync, ConnectionRecord):  # its operations and its local copy go too
            await db.execute(delete(table).where(table.connection_id == conn.id),
                             execution_options={"synchronize_session": False})
        await db.delete(conn)
        await db.commit()
    log.info("connection.deleted", slug=slug, by=actor)
    return {"slug": slug, "deleted": True}


async def list_connections() -> list[dict[str, Any]]:
    async with get_session_factory()() as db:
        rows = (await db.execute(select(Connection).order_by(Connection.name))).scalars().all()
    return [summary(c) for c in rows]


async def get_connection(slug: str) -> dict[str, Any]:
    async with get_session_factory()() as db:
        return summary(await _get(db, slug))


async def list_operations(slug: str, q: str = "", limit: int = 100, offset: int = 0,
                          read_only: bool | None = None, pinned: bool | None = None) -> dict[str, Any]:
    if q.strip():  # ranked search within this connection
        from app.connections.search import search_operations

        async with get_session_factory()() as db:
            await _get(db, slug)  # 404 for unknown connections
        hits = await search_operations(q, connection=slug, read_only=read_only, pinned=pinned,
                                       include_disabled=True, limit=offset + limit)
        return {"connection": slug, "total": len(hits), "operations": hits[offset:]}
    async with get_session_factory()() as db:
        conn = await _get(db, slug)
        stmt = select(Operation).where(Operation.connection_id == conn.id)
        if read_only is not None:
            stmt = stmt.where(Operation.read_only == read_only)
        if pinned is not None:
            stmt = stmt.where(Operation.pinned == pinned)
        total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0)
        rows = (await db.execute(stmt.order_by(Operation.pinned.desc(), Operation.name)
                                 .offset(max(0, offset)).limit(max(1, min(limit, 500))))).scalars().all()
    return {"connection": slug, "total": total, "operations": [op_summary(o) for o in rows]}


async def _find_op(db: Any, conn: Connection, name: str) -> Operation:
    op = (await db.execute(select(Operation).where(Operation.connection_id == conn.id,
                                                   or_(Operation.name == name, Operation.key == name)))).scalars().first()
    if op is None:
        raise CallError(f"No operation '{name}' in '{conn.slug}'", 404)
    return op


async def get_operation(slug: str, name: str) -> dict[str, Any]:
    async with get_session_factory()() as db:
        conn = await _get(db, slug)
        return op_summary(await _find_op(db, conn, name), full=True)


async def update_operation(slug: str, name: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
    async with get_session_factory()() as db:
        conn = await _get(db, slug)
        op = await _find_op(db, conn, name)
        for field in ("pinned", "enabled", "read_only"):
            if field in body:
                setattr(op, field, bool(body[field]))
        conn.updated_at = _now()  # tells the search index to rebuild
        await db.commit()
        log.info("operation.updated", slug=slug, op=op.name, by=actor, fields=sorted(body))
        return op_summary(op, full=True)


# ── Calling ───────────────────────────────────────────────────────────────


def validate_args(schema: dict[str, Any] | None, args: dict[str, Any]) -> None:
    if not isinstance(args, dict):
        raise CallError("Arguments must be an object", 400)
    if not schema:
        return
    try:
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(args), key=lambda e: list(e.path))[:5]
    except SchemaError:
        log.warning("operation.schema_invalid")  # imported schema isn't valid JSON Schema — let the API judge
        return
    if errors:
        msgs = [f"{'/'.join(map(str, e.path)) or '(arguments)'}: {e.message}" for e in errors]
        raise CallError("Invalid arguments: " + "; ".join(msgs), 400, msgs)


def _trim(value: Any) -> tuple[Any, bool]:
    text = json.dumps(value, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return value, False
    if isinstance(value, dict):
        for key in ("data", "rows", "documents", "content"):
            inner = value.get(key)
            if isinstance(inner, list) and inner:
                keep = max(1, int(len(inner) * MAX_RESULT_CHARS / len(text) * 0.9))
                trimmed = {**value, key: inner[:keep], "items_returned": keep, "items_total": len(inner)}
                if len(json.dumps(trimmed, default=str)) <= MAX_RESULT_CHARS:
                    return trimmed, True
    return {"preview": text[:MAX_RESULT_CHARS], "note": "Response truncated — narrow the request"}, True


async def call_operation(slug: str, name: str, args: dict[str, Any], *, approved: bool = False,
                         principal: Principal | None = None, trim: bool = True,
                         audited: bool = True) -> dict[str, Any]:
    """Call one operation as ``principal`` (default: the current request's caller).

    Reads run directly. Writes follow the connection's write mode: ``direct`` runs (Editor + write scope),
    ``approval`` proposes a change request, ``read_only`` refuses. ``approved=True`` is used only by the change
    engine after final approval. Syncs pass ``trim=False`` (they need every record) and ``audited=False`` (they
    write one audit entry per run instead of one per page).
    """
    principal = principal or get_current()
    async with get_session_factory()() as db:
        conn = await _get(db, slug)
        op = await _find_op(db, conn, name)
    who = principal.label() if principal else "system"
    status, error = 200, None
    try:
        if not conn.enabled or not op.enabled:
            raise CallError(f"'{slug}.{op.name}' is disabled", 403)
        if not approved and (principal is None or not principal.allows(slug, READ)):
            raise CallError(f"Your credential may not use '{slug}'", 403)
        validate_args(op.input_schema, args)

        if not op.read_only and not approved:
            from fastapi import HTTPException

            from app.auth.write_mode import APPROVAL, enforce_direct_write, get_write_mode

            if await get_write_mode(slug) == APPROVAL:
                status = 202
                return await _propose(conn, op, args, principal)
            try:
                await enforce_direct_write(slug)  # role + "<slug>:write" scope + write mode "direct"
            except HTTPException as exc:
                raise CallError(str(exc.detail), exc.status_code) from exc

        adapter = get_adapter(conn.kind)
        result = await asyncio.wait_for(adapter.execute(ctx_of(conn), op.binding or {}, args), timeout=CALL_TIMEOUT_S)
        result, truncated = _trim(result) if trim else (result, False)
        return {"ok": True, "connection": slug, "operation": op.name, "result": result, "truncated": truncated}
    except asyncio.TimeoutError as exc:
        status, error = 504, "timeout"
        raise CallError(f"The call took longer than {CALL_TIMEOUT_S} s", 504) from exc
    except UpstreamError as exc:
        status, error = 502, f"upstream {exc.status}"
        raise
    except CallError as exc:
        status, error = exc.status, str(exc)
        raise
    finally:
        if audited:
            await audit.record(principal=who, auth_kind=principal.kind if principal else "internal",
                               source=principal.source if principal else "internal", method="CALL",
                               path=f"/api/connections/{slug}/operations/{op.name}/call",
                               action=f"{slug}.{op.name}", status=status,
                               detail={"args_sha": audit.args_digest(args), "write": not op.read_only,
                                       **({"approved_change": True} if approved else {}),
                                       **({"error": error[:200]} if error else {})})


async def _propose(conn: Connection, op: Operation, args: dict[str, Any], principal: Principal | None) -> dict[str, Any]:
    from app.api.auth_m365 import EXTERNAL_BASE
    from app.services import changes as svc

    who = principal.label() if principal else "system"
    try:
        c = await svc.propose("connection_call", {"connection": conn.slug, "operation": op.name, "args": args},
                              source=principal.source if principal else "api", requested_by=who)
    except svc.ChangeError as exc:
        raise CallError(str(exc), exc.status) from exc
    return {"ok": True, "pending_approval": True, "connection": conn.slug, "operation": op.name,
            "change": svc.summarize(c, EXTERNAL_BASE),
            "next": "This write needs approval: an Editor submits it and an Approver approves it at the url."}
