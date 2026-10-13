"""Approval-mode writes to imported connections: one change request per operation call."""

from __future__ import annotations

from typing import Any

from app.services.commands import Command, CommandError, Plan, register


async def plan_connection_call(p: dict[str, Any]) -> Plan:
    from sqlalchemy import select

    from app.connections.base import CallError
    from app.connections.service import _find_op, validate_args
    from app.db.repository import get_session_factory
    from app.db.schema import Connection

    async with get_session_factory()() as db:
        conn = (await db.execute(select(Connection).where(Connection.slug == p["connection"]))).scalar_one_or_none()
        if conn is None:
            raise CommandError(f"No connection '{p['connection']}'")
        try:
            op = await _find_op(db, conn, p["operation"])
            validate_args(op.input_schema, p.get("args") or {})
        except CallError as exc:
            raise CommandError(str(exc)) from exc
    risk = "delete" if op.method == "DELETE" or op.name.startswith(("delete", "remove")) else \
        "create" if op.method == "POST" or op.name.startswith(("create", "add")) else "update"
    return Plan(
        title=f"{conn.name}: {op.title or op.name}",
        steps=[{"kind": "action", "action": "connection_call", "platform": conn.slug,
                "args": {"connection": conn.slug, "operation": op.name, "args": p.get("args") or {}},
                "description": f"{op.method} {op.target} on {conn.name} ({conn.kind})", "before": None}],
        risk=risk,
        warnings=[] if op.description else ["This operation has no description — check the arguments carefully"],
    )


register(Command(
    "connection_call", "Call an operation on a connected API",
    "Runs one write operation of an imported connection (OpenAPI, Postman, cURL, SOAP, GraphQL, OData, MCP) "
    "after approval. Used automatically for connections whose write mode is 'approval'.",
    "update",
    {"connection": {"type": "string", "description": "Connection slug"},
     "operation": {"type": "string", "description": "Operation name"},
     "args": {"type": "object", "description": "Arguments for the operation"}},
    ["connection", "operation"], plan_connection_call, [],
))
