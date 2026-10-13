"""Workflow executor — runs an :class:`AgentWorkflow` plan.

Each step is one in-process HTTP call against this app, made as the caller who started the workflow.
Steps may reference previous step results via ``{{step.<name>.<field>}}``
placeholders. Parameters from the request flow in as ``{{<param>}}``.

Supports:
- ``dry_run`` mode (returns the resolved plan without executing)
- ``requires_approval`` per step (skipped unless ``approved=True``)
- ``destructive`` flag for visibility
- Binary responses (e.g. PDF) are summarised with ``{kind, bytes}``
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

_INTERNAL_BASE = "http://msp.internal"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w\.|:]+?)\s*\}\}")


# ── Placeholder resolution ───────────────────────────────────────


def _resolve_value(raw: Any, params: dict[str, Any], step_results: dict[str, Any]) -> Any:
    """Substitute ``{{name}}`` and ``{{name|default:value}}`` in a string/dict/list."""
    if isinstance(raw, str):
        def _sub(m: re.Match[str]) -> str:
            token = m.group(1)
            default = ""
            if "|default:" in token:
                token, default = token.split("|default:", 1)
            if token.startswith("step."):
                # step.<name>.<field>...
                parts = token.split(".")[1:]
                cur: Any = step_results
                for p in parts:
                    if isinstance(cur, dict) and p in cur:
                        cur = cur[p]
                    else:
                        return default
                return str(cur)
            if token in params:
                val = params[token]
                return "" if val is None else str(val)
            return default
        return _PLACEHOLDER_RE.sub(_sub, raw)
    if isinstance(raw, dict):
        return {k: _resolve_value(v, params, step_results) for k, v in raw.items()}
    if isinstance(raw, list):
        return [_resolve_value(v, params, step_results) for v in raw]
    return raw


# ── Single step execution ────────────────────────────────────────


async def _execute_step(
    client: httpx.AsyncClient,
    step: dict[str, Any],
    params: dict[str, Any],
    prior: dict[str, Any],
    *,
    approved: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Run one step. Returns a result dict that's appended to step_results."""
    name = step.get("name", "step")
    method = (step.get("method") or "GET").upper()
    endpoint = _resolve_value(step.get("endpoint", ""), params, prior)
    query = _resolve_value(step.get("query", {}) or {}, params, prior)
    body = _resolve_value(step.get("body"), params, prior) if step.get("body") else None
    requires_approval = bool(step.get("requires_approval", False))
    destructive = bool(step.get("destructive", False))
    expect_binary = bool(step.get("expect_binary", False))

    base: dict[str, Any] = {
        "name": name,
        "method": method,
        "endpoint": endpoint,
        "query": query,
        "destructive": destructive,
    }

    if requires_approval and not approved:
        return {**base, "status": "skipped", "reason": "approval_required"}

    if dry_run:
        return {**base, "status": "dry_run", "would_call": f"{method} {endpoint}"}

    url = endpoint if str(endpoint).startswith("/") else f"/{endpoint}"
    started = time.monotonic()
    try:
        if method == "GET":
            resp = await client.get(url, params=query)
        elif method == "POST":
            resp = await client.post(url, params=query, json=body)
        elif method == "PUT":
            resp = await client.put(url, params=query, json=body)
        elif method == "PATCH":
            resp = await client.patch(url, params=query, json=body)
        elif method == "DELETE":
            resp = await client.delete(url, params=query)
        else:
            return {**base, "status": "error", "error": f"unsupported method {method}"}

        latency_ms = int((time.monotonic() - started) * 1000)
        ok = resp.status_code < 400
        result: dict[str, Any] = {
            **base,
            "status": "success" if ok else "error",
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
        }

        if expect_binary or "application/pdf" in resp.headers.get("content-type", ""):
            result["response"] = {
                "kind": "binary",
                "content_type": resp.headers.get("content-type", ""),
                "bytes": len(resp.content),
            }
        else:
            try:
                result["response"] = resp.json()
            except Exception:  # noqa: BLE001
                result["response"] = {"raw": resp.text[:2000]}

        if not ok:
            result["error"] = f"HTTP {resp.status_code}"
        return result
    except Exception as exc:  # noqa: BLE001
        return {
            **base,
            "status": "error",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": str(exc),
        }


# ── Public API ───────────────────────────────────────────────────


async def execute_plan(
    steps: list[dict[str, Any]],
    parameters: dict[str, Any],
    *,
    approved: bool = False,
    dry_run: bool = False,
) -> tuple[str, list[dict[str, Any]], int]:
    """Execute a plan end-to-end.

    Returns ``(outcome, step_results, total_duration_ms)`` where
    ``outcome`` is one of ``"success" | "partial" | "failure"``.
    """
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    prior_by_name: dict[str, Any] = {}

    # In-process, as the caller who started the workflow: every step passes the same auth checks.
    from app.auth.principal import get_current, internal_headers
    from app.main import app as _app

    caller = get_current()
    if caller is None:
        raise PermissionError("Workflows run on behalf of a signed-in caller")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app), base_url=_INTERNAL_BASE,
                                 timeout=_TIMEOUT, headers=internal_headers(caller, "workflow")) as client:
        for step in steps:
            res = await _execute_step(
                client, step, parameters, prior_by_name,
                approved=approved, dry_run=dry_run,
            )
            results.append(res)
            # Make this step's response available to subsequent steps
            prior_by_name[res["name"]] = res.get("response", {})

    duration_ms = int((time.monotonic() - started) * 1000)
    statuses = {r["status"] for r in results}
    if statuses == {"success"} or statuses <= {"success", "dry_run"}:
        outcome = "success"
    elif "success" in statuses:
        outcome = "partial"
    else:
        outcome = "failure"
    return outcome, results, duration_ms
