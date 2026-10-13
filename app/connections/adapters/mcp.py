"""Another MCP server (streamable HTTP) → its tools, served behind this app's auth, scopes, write modes and audit.

Base URL is the remote MCP endpoint (e.g. ``https://vendor.example.com/mcp``); its auth is the connection's auth
(bearer, API key or custom headers). Tools the remote marks ``readOnlyHint: true`` are reads; everything else
is treated as a write.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any

from app.connections.base import (
    CallError,
    ImportResult,
    OperationDef,
    SourceError,
    UpstreamError,
    finalize,
    tool_name,
)
from app.connections.http import ConnCtx, auth_headers_and_params, client

PROTOCOL_VERSION = "2025-06-18"
_SESSION_TTL = 600
_sessions: dict[str, tuple[float, str | None, str]] = {}  # connection id → (expires, session id, protocol)
_ids = iter(range(1, 1 << 62))


def _parse(response_text: str, content_type: str, want_id: int) -> dict[str, Any]:
    if "text/event-stream" in content_type:
        messages = []
        data_lines: list[str] = []
        for line in response_text.splitlines() + [""]:
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif not line.strip() and data_lines:
                with contextlib.suppress(ValueError):
                    messages.append(json.loads("\n".join(data_lines)))
                data_lines = []
        for m in messages:
            if isinstance(m, dict) and m.get("id") == want_id:
                return m
        raise CallError("MCP server sent no response for the request", 502)
    try:
        body = json.loads(response_text)
    except ValueError as exc:
        raise CallError("MCP server did not return JSON", 502, response_text[:500]) from exc
    if isinstance(body, list):
        body = next((m for m in body if isinstance(m, dict) and m.get("id") == want_id), {})
    return body if isinstance(body, dict) else {}


async def _rpc(ctx: ConnCtx, method: str, params: dict[str, Any] | None, session: tuple[str | None, str] | None,
               notify: bool = False) -> tuple[dict[str, Any], str | None]:
    url = ctx.base_url
    if not url:
        raise SourceError("Set the MCP server URL as the connection's base URL")
    auth_h, auth_q = await auth_headers_and_params(ctx)
    headers = {**auth_h, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if session:
        if session[0]:
            headers["Mcp-Session-Id"] = session[0]
        headers["MCP-Protocol-Version"] = session[1]
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    rid = None
    if not notify:
        rid = next(_ids)
        msg["id"] = rid
    async with client() as c:
        r = await c.post(url, json=msg, headers=headers, params=auth_q or None)
    if r.status_code == 404 and session:
        raise _SessionExpired()
    if r.status_code in (401, 403):
        raise CallError(f"MCP server refused the credentials (HTTP {r.status_code})", 502)
    if notify:
        return {}, r.headers.get("mcp-session-id")
    if r.status_code >= 400:
        raise CallError(f"MCP server returned HTTP {r.status_code}", 502, r.text[:500])
    reply = _parse(r.text, r.headers.get("content-type", ""), rid or 0)
    if "error" in reply:
        err = reply["error"] or {}
        raise UpstreamError(f"MCP error {err.get('code')}: {err.get('message')}", 200, err.get("data"))
    return reply.get("result") or {}, r.headers.get("mcp-session-id")


class _SessionExpired(Exception):
    pass


async def _session(ctx: ConnCtx, fresh: bool = False) -> tuple[str | None, str]:
    hit = _sessions.get(ctx.id)
    if hit and hit[0] > time.monotonic() and not fresh:
        return hit[1], hit[2]
    result, sid = await _rpc(ctx, "initialize", {
        "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "msp-api-tools", "title": "MSP API Tools (MCP bridge)", "version": "1.0"},
    }, None)
    proto = str(result.get("protocolVersion") or PROTOCOL_VERSION)
    session = (sid, proto)
    await _rpc(ctx, "notifications/initialized", None, session, notify=True)
    _sessions[ctx.id] = (time.monotonic() + _SESSION_TTL, sid, proto)
    return session


def forget(connection_id: str) -> None:
    _sessions.pop(connection_id, None)


async def _call(ctx: ConnCtx, method: str, params: dict[str, Any]) -> dict[str, Any]:
    for attempt in range(2):
        session = await _session(ctx, fresh=attempt > 0)
        try:
            result, _ = await _rpc(ctx, method, params, session)
            return result
        except _SessionExpired:
            _sessions.pop(ctx.id, None)
    raise CallError("MCP session could not be re-established", 502)


async def discover(ctx: ConnCtx, source: dict[str, Any]) -> ImportResult:
    forget(ctx.id)
    tools: list[dict[str, Any]] = []
    cursor = None
    for _ in range(20):
        result = await _call(ctx, "tools/list", {"cursor": cursor} if cursor else {})
        tools += [t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name")]
        cursor = result.get("nextCursor")
        if not cursor:
            break
    ops = []
    for t in tools:
        ann = t.get("annotations") or {}
        ops.append(OperationDef(
            key=f"tool {t['name']}", name=tool_name(t["name"]), title=str(t.get("title") or ann.get("title") or t["name"]),
            description=str(t.get("description") or "")[:2000], method="CALL", target=t["name"],
            input_schema=t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {"type": "object"},
            output_schema=t.get("outputSchema") if isinstance(t.get("outputSchema"), dict) else None,
            binding={"tool": t["name"]}, read_only=ann.get("readOnlyHint") is True,
        ))
    warnings = [] if ops else ["The MCP server lists no tools"]
    return ImportResult(operations=finalize(ops), base_url=ctx.base_url, warnings=warnings)


async def execute(ctx: ConnCtx, binding: dict[str, Any], args: dict[str, Any]) -> Any:
    result = await _call(ctx, "tools/call", {"name": binding["tool"], "arguments": args or {}})
    content = result.get("content") or []
    if result.get("isError"):
        text = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        raise UpstreamError(f"Remote tool failed: {text[:500]}", 200, content)
    out: dict[str, Any] = {"content": content}
    if result.get("structuredContent") is not None:
        out["structured"] = result["structuredContent"]
    return out
