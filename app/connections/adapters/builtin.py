"""Built-in integrations (ConnectWise PSA, Asio, ScreenConnect, SentinelOne, ESET …) as connections.

Their operations come from the vendor's OpenAPI document (fetched by the API explorer, or bundled with the app
in ``app/specs``); calls go through the platform's own connector, which knows that vendor's sign-in (PSA basic +
clientId, Asio OAuth, ScreenConnect's header, …). Writes are checked by the connection call path like any other
connection. Provided by the MSP pack (``app/services/builtin_integrations.py``).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import quote

from app.connections.base import CallError, ImportResult, UpstreamError
from app.connections.http import ConnCtx


def _platform(ctx: ConnCtx) -> str:
    return str((ctx.settings or {}).get("platform") or ctx.slug)


async def load_spec(platform: str) -> dict[str, Any] | None:
    """The vendor's OpenAPI document: the explorer's stored copy, else the one bundled with the app."""
    try:
        from sqlalchemy import select

        from app.db.repository import get_session_factory
        from app.db.schema import ApiSpec

        async with get_session_factory()() as db:
            row = (await db.execute(select(ApiSpec).where(ApiSpec.platform == platform))).scalar_one_or_none()
        if row is not None and row.spec_json:
            return dict(row.spec_json)
    except Exception:  # noqa: BLE001 — no explorer tables: use the bundled file
        pass
    try:
        from app.api.api_discovery import _LOCAL_SPEC_FILES, _SPECS_DIR, load_local_spec

        name = _LOCAL_SPEC_FILES.get(platform)
        if name and (_SPECS_DIR / name).exists():
            return dict(load_local_spec(_SPECS_DIR / name))
    except Exception:  # noqa: BLE001
        pass
    return None


async def discover(ctx: ConnCtx, source: dict[str, Any]) -> ImportResult:
    from app.connections.adapters.openapi import build_operations

    platform = _platform(ctx)
    spec = await load_spec(platform)
    if not spec:
        return ImportResult(operations=[], warnings=[f"No API definition is available for {platform} yet"])
    ops, refs = await asyncio.to_thread(build_operations, spec)  # PSA's definition has ~3000 operations
    info = spec.get("info") or {}
    warnings = [f"{refs.external} external $ref(s) were not followed"] if refs.external else []
    return ImportResult(operations=ops, name=str(info.get("title") or ""),
                        description=str(info.get("description") or "")[:2000], warnings=warnings)


async def execute(ctx: ConnCtx, binding: dict[str, Any], args: dict[str, Any]) -> Any:
    from fastapi import HTTPException

    from app.api.proxy import vendor_request

    method = str(binding.get("method") or "GET").upper()
    path = str(binding.get("path") or "")
    query: dict[str, Any] = {}
    for p in binding.get("params") or []:
        value = args.get(p.get("arg") or p["name"])
        if value is None:
            value = p.get("default")
        if value is None:
            continue
        where = p.get("in", "query")
        if where == "path":
            path = path.replace("{" + p["name"] + "}", quote(str(value), safe=""))
        elif where == "query":
            query[p["name"]] = json.dumps(value) if isinstance(value, dict | list) else value
    if missing := re.findall(r"\{([^{}/]+)\}", path):
        raise CallError(f"Missing path parameter(s): {', '.join(missing)}", 400)
    body_spec = binding.get("body") or {}
    body = args.get(body_spec.get("arg") or "body") if body_spec else None
    try:
        result = await vendor_request(_platform(ctx), method, path, query or None, body)
    except HTTPException as exc:
        if exc.status_code in (400, 404) and "not found or not configured" in str(exc.detail):
            raise CallError(f"{_platform(ctx)} isn't connected — add its credentials first", 409) from exc
        raise UpstreamError(f"{_platform(ctx)} answered {exc.status_code}", exc.status_code, exc.detail) from exc
    return result.get("response", result)
