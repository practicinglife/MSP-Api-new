"""One way to call any vendor API — used only by the change engine (never cached).

``request(platform, method, path, params, body)`` returns ``{"status": int, "body": Any, "ok": bool}``
and never raises on vendor 4xx/5xx, so results can be recorded faithfully.
Auth reuses each connector's existing pattern (``proxy._resolve_auth``) and the CW Manage gateway headers.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.api.connector_health import get_connectors
from app.api.proxy import _resolve_auth
from app.services import cwm_gateway

log = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
PLATFORMS = ("connectwise_psa", "asio", "screenconnect", "sentinelone", "eset")


def _connector(platform: str) -> Any:
    for c in get_connectors():
        if c.platform_name == platform:
            return c
    raise ValueError(f"No connector configured for {platform}")


async def _base_and_headers(platform: str) -> tuple[str, dict[str, str]]:
    if platform == "connectwise_psa":
        return cwm_gateway._base_url(), cwm_gateway._auth_headers()  # noqa: SLF001
    c = _connector(platform)
    await c.authenticate()
    base, headers = _resolve_auth(c)
    if platform == "asio" and hasattr(c, "_api_base"):
        base = c._api_base()  # noqa: SLF001
    if not base:
        raise ValueError(f"Cannot determine base URL for {platform}")
    return base.rstrip("/"), headers


async def request(
    platform: str,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    body: Any = None,
) -> dict[str, Any]:
    method = method.upper()
    if not path.startswith("/"):
        path = "/" + path
    try:
        base, headers = await _base_and_headers(platform)
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "ok": False, "body": {"error": f"auth/config: {exc}"}}
    headers = {**headers, "Accept": "application/json"}
    if body is not None:
        headers.setdefault("Content-Type", "application/json")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(method, base + path, headers=headers, params=params or None,
                                        json=body if body is not None else None)
    except Exception as exc:  # noqa: BLE001
        log.warning("vendor_client.error", platform=platform, method=method, path=path, error=str(exc))
        return {"status": 0, "ok": False, "body": {"error": str(exc)}}
    try:
        parsed: Any = resp.json() if resp.content else None
    except ValueError:
        parsed = {"raw": resp.text[:5000]}
    log.info("vendor_client.call", platform=platform, method=method, path=path, status=resp.status_code)
    return {"status": resp.status_code, "ok": 200 <= resp.status_code < 300, "body": parsed}


async def get(platform: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return await request(platform, "GET", path, params=params)
