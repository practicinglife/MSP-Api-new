"""The MCP servers built from connections.

* ``/mcp/{slug}`` — one connection: ``search_operations``, ``describe_operation``, ``call_operation``, plus every
  pinned operation as its own typed tool (its JSON-Schema is the tool's input schema). The operation list is
  also a resource.
* ``/mcp/{slug}/{group}`` — a hand-picked set of that connection's operations (``settings.mcp_groups``), each as
  a typed tool; search / describe / call stay inside the set.
* ``/mcp`` — the hub: ``list_connections`` and the same three tools across every connection the caller may use.

Search-first keeps large APIs (thousands of operations) usable by AI clients. Every call goes through
``service.call_operation``: read scope, argument validation, write mode (read-only / direct / approval) and audit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from app.auth import audit
from app.auth.principal import READ, Principal
from app.auth.write_mode import APPROVAL, DIRECT, READ_ONLY, get_write_mode
from app.connections import service
from app.connections.base import CallError, SourceError, UpstreamError
from app.db.repository import get_session_factory
from app.db.schema import Connection, Operation
from app.mcp.protocol import Server

VERSION = "1.0"
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
BUILTIN = ("search_operations", "describe_operation", "call_operation", "list_connections", "search_records",
           "get_record")
MAX_TYPED_TOOLS = 100
MAX_RESOURCE_OPS = 5000

_MODE_TEXT = {
    READ_ONLY: "Writes are refused: this connection is read-only.",
    DIRECT: "Writes run immediately for callers with the Editor role and a write scope.",
    APPROVAL: "Writes become change requests: nothing runs until a person approves it (the call returns the link).",
}


@dataclass
class Caller:
    principal: Principal
    path: str
    ip: str | None
    base_url: str


class Unavailable(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


def _str(desc: str, max_len: int = 200) -> dict[str, Any]:
    return {"type": "string", "description": desc, "maxLength": max_len}


def groups_of(conn: Connection) -> dict[str, list[str]]:
    raw = (conn.settings or {}).get("mcp_groups") or {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name, ops in raw.items():
        if isinstance(name, str) and GROUP_RE.match(name):
            names = ops.get("operations") if isinstance(ops, dict) else ops
            if isinstance(names, list):
                out[name] = [str(n) for n in names][:MAX_TYPED_TOOLS]
    return out


async def connection_for(slug: str, principal: Principal, group: str | None = None) -> Connection:
    """The connection behind ``/mcp/{slug}[/{group}]``, or ``Unavailable`` (404 / 403)."""
    async with get_session_factory()() as db:
        conn = (await db.execute(select(Connection).where(Connection.slug == slug))).scalar_one_or_none()
    if conn is None or not conn.enabled:
        raise Unavailable(404, f"No enabled connection '{slug}'")
    if not principal.allows(slug, READ):
        raise Unavailable(403, f"Your credential may not use '{slug}'")
    if group is not None and group not in groups_of(conn):
        raise Unavailable(404, f"No group '{group}' in '{slug}'")
    return conn


async def _typed_ops(conn: Connection, names: list[str] | None) -> list[Operation]:
    stmt = select(Operation).where(Operation.connection_id == conn.id, Operation.enabled.is_(True))
    stmt = stmt.where(Operation.pinned.is_(True)) if names is None else stmt.where(Operation.name.in_(names or [""]))
    async with get_session_factory()() as db:
        return list((await db.execute(stmt.order_by(Operation.name).limit(MAX_TYPED_TOOLS))).scalars())


def _typed_tool(op: Operation, tool: str) -> dict[str, Any]:
    schema = dict(op.input_schema) if isinstance(op.input_schema, dict) else {}
    schema.setdefault("properties", {})
    schema["type"] = "object"
    desc = op.title if op.title and op.title != op.name else ""
    if op.description:
        desc = f"{desc}. {op.description}" if desc else op.description
    desc = (desc[:1000] + f" [{op.method} {op.target}]").strip()
    if not op.read_only:
        desc += " Changes data (follows the connection's write mode)."
    method = (op.method or "").upper()
    return {"name": tool, "description": desc, "inputSchema": schema,
            "annotations": {"title": op.title or op.name, "readOnlyHint": bool(op.read_only),
                            "destructiveHint": not op.read_only and method in ("DELETE", "PUT"),
                            "openWorldHint": True}}


def _builtin_tools(hub: bool, scope_name: str) -> list[dict[str, Any]]:
    conn_arg = {"connection": _str("Connection slug (from list_connections or search results)", 64)}
    search_props: dict[str, Any] = {
        "query": _str("What you want to do, in plain words (synonyms and typos are fine)", 300),
        "read_only": {"type": "boolean", "description": "true = only reads, false = only writes"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Max results (default 10)"},
    }
    if hub:
        search_props["connection"] = _str("Only this connection (slug)", 64)
    op_arg = {"operation": _str("Operation name from search_operations", 255)}
    tools = [
        {"name": "search_operations",
         "description": f"Search the operations of {scope_name} by what you want to do. Returns operation names "
                        "(ranked) to use with describe_operation and call_operation.",
         "inputSchema": _obj(search_props, ["query"]),
         "annotations": {"readOnlyHint": True, "openWorldHint": False}},
        {"name": "describe_operation",
         "description": "Full description and argument schema of one operation. Check this before calling.",
         "inputSchema": _obj({**(conn_arg if hub else {}), **op_arg}, (["connection"] if hub else []) + ["operation"]),
         "annotations": {"readOnlyHint": True, "openWorldHint": False}},
        {"name": "call_operation",
         "description": "Run one operation with arguments that match its input schema. Reads run directly; writes "
                        "follow the connection's write mode (refused, run, or turned into an approval request).",
         "inputSchema": _obj({**(conn_arg if hub else {}), **op_arg,
                              "args": {"type": "object", "description": "Arguments for the operation"}},
                             (["connection"] if hub else []) + ["operation"]),
         "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}},
    ]
    if hub:
        tools.insert(0, {"name": "list_connections",
                         "description": "Every API connected here that you may use: slug, kind, operation count, "
                                        "write mode and its own MCP server URL.",
                         "inputSchema": _obj({}),
                         "annotations": {"readOnlyHint": True, "openWorldHint": False}})
    return tools


def _record_tools(hub: bool, operations: list[str] | None) -> list[dict[str, Any]]:
    """Tools over the local copy (connection_records) — instant, no API call."""
    conn_arg = {"connection": _str("Connection slug (from list_connections)", 64)}
    op_arg: dict[str, Any] = {"type": "string", "description": "Only records kept from this operation"}
    if operations:
        op_arg["enum"] = operations
    props: dict[str, Any] = {
        **(conn_arg if hub else {}),
        "query": _str("Words to look for in the stored records: names, numbers, emails, hostnames …", 300),
        "filters": {"type": "object", "description": "Exact matches on record fields (dotted paths), e.g. "
                                                     '{"status": "open", "company.id": 42}. A list means any of.'},
        "operation": op_arg,
        "include_removed": {"type": "boolean", "description": "Also records the API no longer returns"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Max results (default 10)"},
    }
    return [
        {"name": "search_records",
         "description": "Search the LOCAL COPY of data kept from " + ("the connections" if hub else "this API")
                        + " by scheduled syncs: fast, no API call. Give a query, filters or both. Each record "
                          "shows when it was last seen; use call_operation when you need live data.",
         "inputSchema": _obj(props), "annotations": {"readOnlyHint": True, "openWorldHint": False}},
        {"name": "get_record",
         "description": "One stored record by its key (from search_records) — no API call.",
         "inputSchema": _obj({**(conn_arg if hub else {}), "key": _str("Record key", 255), "operation": op_arg},
                             (["connection"] if hub else []) + ["key"]),
         "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    ]


def _fresh_text(fresh: list[dict[str, Any]]) -> str:
    def when(dt: Any) -> str:
        return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "not yet"
    return "; ".join(f"{f['operation']}: {f['records']} records, every {f['every_minutes']} min, last synced "
                     f"{when(f['last_synced'])}" for f in fresh)


async def _records_call(tool: str, args: dict[str, Any], conns: list[Connection],
                        allowed_ops: set[str] | None = None) -> tuple[bool, Any]:
    from app.connections import sync as local

    if tool == "get_record":
        rec = await local.get_record(conns[0], str(args["key"]), args.get("operation")) if conns else None
        if rec is None or (allowed_ops is not None and rec["operation"] not in allowed_ops):
            return False, f"No stored record '{args['key']}'"
        return True, jsonable_encoder(rec)
    if not (args.get("query") or args.get("filters")):
        return False, "Give a query, filters or both"
    filters = args.get("filters") or {}
    res = await local.search_records(conns, str(args.get("query") or ""), filters=filters,
                                     operation=args.get("operation"), include_removed=bool(args.get("include_removed")),
                                     limit=200 if allowed_ops is not None else _limit(args))
    if allowed_ops is not None:
        res["records"] = [r for r in res["records"] if r["operation"] in allowed_ops][:_limit(args)]
        res["count"], res["total"] = len(res["records"]), None
    return True, jsonable_encoder(res)


async def _run_call(slug: str, name: str, args: Any, principal: Principal) -> tuple[bool, Any]:
    if not isinstance(args, dict):
        return False, "args must be an object"
    try:
        out = await service.call_operation(slug, name, args, principal=principal)
    except UpstreamError as exc:
        return False, {"error": str(exc), "upstream_status": exc.status, "upstream": exc.detail}
    except (CallError, SourceError) as exc:
        detail = getattr(exc, "detail", None)
        return False, {"error": str(exc), **({"details": detail} if detail else {})}
    if out.get("pending_approval"):
        return True, jsonable_encoder(out)
    return True, jsonable_encoder({"result": out["result"], **({"truncated": True} if out.get("truncated") else {})})


async def _describe(slug: str, name: str) -> tuple[bool, Any]:
    try:
        op = await service.get_operation(slug, name)
    except CallError as exc:
        return False, str(exc)
    if not op.get("enabled", True):
        return False, f"'{name}' is disabled"
    op.pop("key", None)
    if not op["read_only"]:
        op["write_mode"] = _MODE_TEXT[await get_write_mode(slug)]
    op["how_to_call"] = f"call_operation with operation='{op['name']}' and args matching input_schema"
    return True, op


def _hits(hits: list[dict[str, Any]], hub: bool) -> dict[str, Any]:
    rows = [{**({"connection": h["connection"]} if hub else {}), "operation": h["name"], "title": h["title"],
             "method": h["method"], "target": h["target"], "read_only": h["read_only"],
             **({"pinned": True} if h["pinned"] else {}), "description": h["description"]} for h in hits]
    return {"count": len(rows), "operations": rows,
            "next": "describe_operation for the arguments, then call_operation." if rows else
                    "Nothing matched. Try other words, or fewer of them."}


async def _audit(caller: Caller, action: str, ok: bool, args: dict[str, Any]) -> None:
    p = caller.principal
    await audit.record(principal=p.label(), auth_kind=p.kind, source="mcp", method="CALL", path=caller.path,
                       action=action, status=200 if ok else 400, ip=caller.ip,
                       detail={"args_sha": audit.args_digest(args)})


def _limit(args: dict[str, Any]) -> int:
    return max(1, min(int(args.get("limit") or 10), 50))


# ── /mcp/{slug} and /mcp/{slug}/{group} ───────────────────────────────────


async def connection_server(conn: Connection, caller: Caller, group: str | None = None) -> Server:
    slug = conn.slug
    group_names = groups_of(conn)[group] if group else None
    ops = await _typed_ops(conn, group_names)
    typed: dict[str, str] = {}  # tool name → operation name
    tools = _builtin_tools(False, f"{conn.name}" + (f" (group '{group}')" if group else ""))
    for op in ops:
        tool = op.name if op.name not in BUILTIN else f"op_{op.name}"
        if TOOL_NAME_RE.match(tool) and tool not in typed:
            typed[tool] = op.name
            tools.append(_typed_tool(op, tool))
    allowed_ops = set(group_names) if group_names is not None else None
    mode = await get_write_mode(slug)

    def outside(name: str) -> bool:
        return allowed_ops is not None and name not in allowed_ops

    from app.connections import sync as local

    fresh = [f for f in await local.freshness(conn) if not outside(f["operation"])]
    kept_ops = {f["operation"] for f in fresh}
    if fresh:
        tools += _record_tools(False, sorted(kept_ops))

    async def call(tool: str, args: dict[str, Any]) -> tuple[bool, Any]:
        if tool in typed:  # the call itself is audited by service.call_operation
            return await _run_call(slug, typed[tool], args, caller.principal)
        spec = next(t for t in tools if t["name"] == tool)
        try:
            service.validate_args(spec["inputSchema"], args)
        except CallError as exc:
            return False, str(exc)
        name = str(args.get("operation", ""))
        if tool == "call_operation":
            if outside(name):
                return False, f"'{name}' is not part of this server"
            return await _run_call(slug, name, args.get("args") or {}, caller.principal)
        if tool in ("search_records", "get_record"):
            ok, data = await _records_call(tool, args, [conn], kept_ops if allowed_ops is not None else None)
        elif tool == "describe_operation":
            ok, data = (False, f"'{name}' is not part of this server") if outside(name) else await _describe(slug, name)
        else:
            from app.connections.search import search_operations

            hits = await search_operations(str(args["query"]), connection=slug, read_only=args.get("read_only"),
                                           limit=200 if allowed_ops is not None else _limit(args))
            if allowed_ops is not None:
                hits = [h for h in hits if h["name"] in allowed_ops][:_limit(args)]
            ok, data = True, _hits(hits, hub=False)
        await _audit(caller, f"{slug}:{tool}", ok, args)
        return ok, data

    uri = f"api://{slug}/operations" + (f"/{group}" if group else "")

    async def read_resource(requested: str) -> tuple[str, str] | None:
        if requested != uri:
            return None
        stmt = select(Operation.name, Operation.title, Operation.method, Operation.target, Operation.read_only) \
            .where(Operation.connection_id == conn.id, Operation.enabled.is_(True))
        if group_names is not None:
            stmt = stmt.where(Operation.name.in_(group_names or [""]))
        async with get_session_factory()() as db:
            rows = (await db.execute(stmt.order_by(Operation.name).limit(MAX_RESOURCE_OPS))).all()
        return "application/json", json.dumps([{"name": r.name, "title": r.title, "method": r.method,
                                                "target": r.target, "read_only": r.read_only} for r in rows])

    count = len(group_names) if group_names is not None else conn.operation_count
    pinned = (f" These operations are also tools of their own: {', '.join(sorted(typed))}." if typed else "")
    instructions = (
        f"{conn.name} ({conn.kind}){': ' + conn.description[:1500] if conn.description else ''}\n\n"
        f"This MCP server is the '{slug}' connection{f', group {group!r}' if group else ''} ({count} operations). "
        "Find the right operation with search_operations, check its arguments with describe_operation, then run it "
        f"with call_operation.{pinned} {_MODE_TEXT[mode]} Results come live from the API."
    )
    if fresh:
        instructions += (
            "\n\nLocal copy (search_records / get_record: instant, no API call) — " + _fresh_text(fresh) + ". "
            "Look things up there first; call the API when the user needs live data or something not kept."
        )
    return Server(
        name=f"{slug}-{group}" if group else slug, title=conn.name + (f" — {group}" if group else ""),
        version=VERSION, instructions=instructions, tools=tools, call=call,
        resources=[{"uri": uri, "name": "operations", "title": f"{conn.name} operations",
                    "description": "Every enabled operation: name, title, method, target, read-only.",
                    "mimeType": "application/json"}],
        read_resource=read_resource,
    )


# ── /mcp (hub) ────────────────────────────────────────────────────────────


async def usable_connections(principal: Principal) -> list[Connection]:
    async with get_session_factory()() as db:
        rows = (await db.execute(select(Connection).where(Connection.enabled.is_(True))
                                 .order_by(Connection.name))).scalars().all()
    return [c for c in rows if principal.allows(c.slug, READ)]


async def hub_server(caller: Caller) -> Server:
    from app.connections import sync as local

    principal = caller.principal
    tools = _builtin_tools(True, "every connected API")
    kept = await local.connections_with_records(principal)
    if kept:
        tools += _record_tools(True, None)

    async def usable(slug: str) -> bool:
        return any(c.slug == slug for c in await usable_connections(principal))

    async def call(tool: str, args: dict[str, Any]) -> tuple[bool, Any]:
        spec = next(t for t in tools if t["name"] == tool)
        try:
            service.validate_args(spec["inputSchema"], args)
        except CallError as exc:
            return False, str(exc)
        slug = str(args.get("connection") or "")
        if tool == "call_operation":
            return await _run_call(slug, str(args["operation"]), args.get("args") or {}, principal)
        if tool in ("search_records", "get_record"):
            conns = [c for c in kept if not slug or c.slug == slug]
            if slug and not conns:
                ok, data = False, f"'{slug}' keeps no local copy you may read"
            else:
                ok, data = await _records_call(tool, args, conns)
        elif tool == "list_connections":
            out = []
            for c in await usable_connections(principal):
                out.append({"connection": c.slug, "name": c.name, "kind": c.kind, "operations": c.operation_count,
                            "description": (c.description or "")[:200], "write_mode": await get_write_mode(c.slug),
                            "mcp_url": f"{caller.base_url}/mcp/{c.slug}",
                            **({"local_copy": jsonable_encoder(await local.freshness(c))}
                               if any(k.id == c.id for k in kept) else {})})
            ok, data = True, {"count": len(out), "connections": out}
        elif tool == "describe_operation":
            ok, data = await _describe(slug, str(args["operation"])) if await usable(slug) else \
                (False, f"No connection '{slug}' you may use")
        else:
            from app.connections.search import search_operations

            hits = await search_operations(str(args["query"]), connection=slug or None, read_only=args.get("read_only"),
                                           limit=_limit(args), allowed=lambda s: principal.allows(s, READ))
            ok, data = True, _hits(hits, hub=True)
        await _audit(caller, f"hub:{tool}", ok, args)
        return ok, data

    conns = await usable_connections(principal)
    listed = ", ".join(f"{c.slug} ({c.operation_count})" for c in conns[:40]) or "none yet"
    instructions = (
        "Every API connected to this server, in one place. Connections (operations): " + listed + ".\n\n"
        "Use search_operations with what you want to do (optionally one connection), describe_operation for the "
        "arguments, then call_operation. list_connections shows each connection's write mode and its own MCP "
        "server URL. Reads run live; writes follow each connection's write mode."
    )
    if kept:
        instructions += ("\n\nLocal copies (search_records / get_record: instant, no API call) are kept for: "
                         + ", ".join(c.slug for c in kept) + ". Look things up there first.")
    return Server(name="api-hub", title="All connected APIs", version=VERSION, instructions=instructions,
                  tools=tools, call=call)
