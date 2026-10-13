"""API Schema Discovery router — fetch, parse, store, search, and execute OpenAPI specs.

Loads specs from local files first (app/specs/), falling back to remote URLs
for platforms like ESET that have live public specs.
"""

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.api.connector_health import get_connectors
from app.auth.principal import get_current, internal_headers
from app.auth.write_mode import enforce_direct_write
from app.db.repository import ApiDiscoveryRepository, get_session_factory
from app.services.openapi_parser import (
    fetch_openapi_spec,
    load_local_spec,
    parse_openapi_spec,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/discovery", tags=["api-discovery"])

# Directory containing local spec files (shipped inside the Docker image).
_SPECS_DIR = Path(__file__).resolve().parent.parent / "specs"

# Mapping: platform_name → local spec filename inside _SPECS_DIR.
# These are loaded from disk — no network call required.
_LOCAL_SPEC_FILES: dict[str, str] = {
    "connectwise_psa": "connectwise_psa.json",
    "sentinelone": "sentinelone.json",
    "asio": "asio.json",
    "screenconnect": "screenconnect.json",
    "eset": "eset.json",
}

# Fallback remote URLs for platforms whose specs are accessible online.
# Used only when no local file exists (e.g. ESET provides a live spec).
_REMOTE_SWAGGER_URLS: dict[str, str] = {
    "eset": "/swagger/v1/swagger.json",
}

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# The app's own spec — always available at runtime via FastAPI's built-in route.
_SELF_PLATFORM = "msp_api_tools"


async def _scan_self() -> dict[str, Any]:
    """Scan the app's own OpenAPI spec (read in-process — /openapi.json now needs sign-in) and store it."""
    start = time.monotonic()
    try:
        from app.main import app as _app

        return await _store_and_respond(_SELF_PLATFORM, _app.openapi(), "self:openapi()", start)
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.monotonic() - start) * 1000)
        log.warning("discovery.scan_self.failed", error=str(exc))
        return {
            "platform": _SELF_PLATFORM,
            "status": "error",
            "error": str(exc),
            "latency_ms": latency_ms,
        }


async def seed_self_spec(spec_json: dict[str, Any]) -> None:
    """Store the app's own OpenAPI spec directly from the FastAPI schema dict.

    Called from the lifespan startup so the spec is always pre-populated
    without requiring the HTTP server to be up or a manual scan trigger.
    """
    start = time.monotonic()
    try:
        await _store_and_respond(_SELF_PLATFORM, spec_json, "self:openapi()", start)
        log.info("discovery.self_spec_seeded", platform=_SELF_PLATFORM)
    except Exception as exc:  # noqa: BLE001
        log.warning("discovery.self_spec_seed_failed", error=str(exc))


def _build_swagger_url(connector: Any) -> str | None:
    """Resolve the full Swagger URL for a connector (remote fallback only)."""
    platform = connector.platform_name

    # Check the connector itself for a swagger_url attribute
    custom_url = getattr(connector, "swagger_url", "")
    if custom_url:
        return custom_url

    # Fall back to the known remote mapping
    url_or_path = _REMOTE_SWAGGER_URLS.get(platform)
    if not url_or_path:
        return None

    # Absolute URLs (https://...) are used directly
    if url_or_path.startswith(("http://", "https://")):
        return url_or_path

    # Relative paths are appended to the connector's base URL
    base = ""
    if hasattr(connector, "_base_url"):
        base = connector._base_url()
    elif hasattr(connector, "_settings"):
        settings = connector._settings
        if platform == "eset":
            base = settings.eset_base_url.rstrip("/")
        elif platform == "sentinelone":
            base = settings.s1_base_url.rstrip("/")
    if not base:
        return None
    return f"{base}{url_or_path}"


async def _scan_single_connector(connector: Any) -> dict[str, Any]:
    """Load spec from local file first, then try remote URL as fallback."""
    platform = connector.platform_name
    start = time.monotonic()

    try:
        # --- Try local file first ---
        local_file = _LOCAL_SPEC_FILES.get(platform)
        if local_file:
            local_path = _SPECS_DIR / local_file
            if local_path.exists():
                spec_json = load_local_spec(local_path)
                source = f"local:{local_file}"
                return await _store_and_respond(
                    platform, spec_json, source, start,
                )

        # --- Fallback to remote URL ---
        swagger_url = _build_swagger_url(connector)
        if not swagger_url:
            return {
                "platform": platform,
                "status": "skipped",
                "reason": "No local spec file and no known remote Swagger URL",
            }

        spec_json = await fetch_openapi_spec(swagger_url)
        return await _store_and_respond(
            platform, spec_json, swagger_url, start,
        )

    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.monotonic() - start) * 1000)
        log.warning("discovery.scan.failed", platform=platform, error=str(exc))
        return {
            "platform": platform,
            "status": "error",
            "error": str(exc),
            "latency_ms": latency_ms,
        }


async def _store_and_respond(
    platform: str,
    spec_json: dict[str, Any],
    source: str,
    start: float,
) -> dict[str, Any]:
    """Parse, persist, and build the success response for a scanned spec."""
    endpoints = parse_openapi_spec(spec_json)
    info = spec_json.get("info", {})

    factory = get_session_factory()
    async with factory() as session:
        repo = ApiDiscoveryRepository(session)
        await repo.upsert_spec(
            platform=platform,
            title=info.get("title", platform),
            version=info.get("version", ""),
            swagger_url=source,
            spec_json=spec_json,
            endpoint_count=len(endpoints),
        )
        await repo.replace_endpoints(platform, endpoints)

    latency_ms = int((time.monotonic() - start) * 1000)
    log.info(
        "discovery.scan.success",
        platform=platform,
        endpoints=len(endpoints),
        source=source,
        latency_ms=latency_ms,
    )
    return {
        "platform": platform,
        "status": "ok",
        "title": info.get("title", ""),
        "version": info.get("version", ""),
        "endpoint_count": len(endpoints),
        "source": source,
        "latency_ms": latency_ms,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/scan")
async def scan_all_specs() -> dict[str, Any]:
    """Trigger OpenAPI spec discovery for all registered connectors (concurrent).

    Also scans the application's own spec (platform ``msp_api_tools``) so the
    API Explorer can browse all internal endpoints without leaving the dashboard.
    """
    connectors = get_connectors()

    tasks = [_scan_single_connector(c) for c in connectors]
    # Always include the app's own spec
    tasks.append(_scan_self())
    results = await asyncio.gather(*tasks)

    ok_count = sum(1 for r in results if r.get("status") == "ok")
    return {
        "total_connectors": len(connectors) + 1,
        "scanned": ok_count,
        "skipped": sum(1 for r in results if r.get("status") == "skipped"),
        "errors": sum(1 for r in results if r.get("status") == "error"),
        "results": results,
    }


@router.get("/specs")
async def list_specs() -> list[dict[str, Any]]:
    """Return all stored API specs (without the full JSON blob)."""
    factory = get_session_factory()
    async with factory() as session:
        repo = ApiDiscoveryRepository(session)
        specs = await repo.list_specs()
        return [
            {
                "id": s.id,
                "platform": s.platform,
                "title": s.title,
                "version": s.version,
                "swagger_url": s.swagger_url,
                "endpoint_count": s.endpoint_count,
                "fetched_at": s.fetched_at.isoformat() if s.fetched_at else None,
            }
            for s in specs
        ]


@router.get("/specs/{platform}/endpoints")
async def list_platform_endpoints(platform: str) -> list[dict[str, Any]]:
    """Return all endpoints for a specific platform."""
    factory = get_session_factory()
    async with factory() as session:
        repo = ApiDiscoveryRepository(session)
        endpoints = await repo.list_endpoints(platform)
        if not endpoints:
            raise HTTPException(status_code=404, detail=f"No endpoints found for '{platform}'")
        return [
            {
                "id": ep.id,
                "path": ep.path,
                "method": ep.method,
                "summary": ep.summary,
                "description": ep.description,
                "tags": ep.tags,
                "parameters": ep.parameters,
                "request_body_schema": ep.request_body_schema,
                "response_schema": ep.response_schema,
            }
            for ep in endpoints
        ]


@router.get("/search")
async def search_endpoints(q: str = Query(..., min_length=1)) -> list[dict[str, Any]]:
    """Search endpoints across all platforms by keyword."""
    factory = get_session_factory()
    async with factory() as session:
        repo = ApiDiscoveryRepository(session)
        endpoints = await repo.search_endpoints(q)
        return [
            {
                "id": ep.id,
                "platform": ep.platform,
                "path": ep.path,
                "method": ep.method,
                "summary": ep.summary,
                "tags": ep.tags,
            }
            for ep in endpoints
        ]


class ExecuteRequest(BaseModel):
    """Request body for executing an API call against a platform."""

    platform: str
    path: str
    method: str = "GET"
    params: dict[str, Any] | None = None
    body: dict[str, Any] | None = None
    use_cache: bool = True
    cache_ttl_days: int = 365


@router.post("/execute")
async def execute_endpoint(req: ExecuteRequest, request: Request) -> dict[str, Any]:
    """Execute an API call on a connector platform.

    If ``use_cache`` is True and a cached response exists, it is returned
    immediately without making a network call (GET only — writes always run live).
    Results are cached with the specified TTL.
    """
    factory = get_session_factory()
    is_read = req.method.upper() == "GET"
    if not is_read and req.platform != _SELF_PLATFORM:
        await enforce_direct_write(req.platform)

    # Check cache first
    if req.use_cache and is_read:
        async with factory() as session:
            repo = ApiDiscoveryRepository(session)
            cached = await repo.get_cached_response(req.platform, req.path, req.method)
            if cached:
                return {
                    "platform": req.platform,
                    "path": req.path,
                    "method": req.method,
                    "source": "cache",
                    "status_code": cached.status_code,
                    "cached_at": cached.cached_at.isoformat() if cached.cached_at else None,
                    "expires_at": cached.expires_at.isoformat() if cached.expires_at else None,
                    "response": cached.response_body,
                }

    # Find the connector
    connectors = get_connectors()
    connector = next((c for c in connectors if c.platform_name == req.platform), None)

    # Self-platform: call this app's own API in-process, as the same caller (the middleware re-checks it)
    if req.platform == _SELF_PLATFORM:
        url = req.path if req.path.startswith("/") else f"/{req.path}"
        start = time.monotonic()
        caller = get_current()
        if caller is None:
            raise HTTPException(status_code=401, detail="Sign-in required")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=request.app),
                                         base_url="http://msp.internal", timeout=_TIMEOUT,
                                         headers=internal_headers(caller, "api-explorer")) as client:
                if req.method.upper() == "GET":
                    resp = await client.get(url, params=req.params)
                elif req.method.upper() == "POST":
                    resp = await client.post(url, json=req.body, params=req.params)
                elif req.method.upper() == "PUT":
                    resp = await client.put(url, json=req.body, params=req.params)
                elif req.method.upper() == "DELETE":
                    resp = await client.delete(url, params=req.params)
                else:
                    raise HTTPException(status_code=400, detail=f"Unsupported method: {req.method}")

                latency_ms = int((time.monotonic() - start) * 1000)
                try:
                    resp_body = resp.json()
                except Exception:  # noqa: BLE001
                    resp_body = {"raw": resp.text[:5000]}

                async with factory() as session:
                    repo = ApiDiscoveryRepository(session)
                    await repo.cache_response(
                        platform=req.platform,
                        endpoint_path=req.path,
                        method=req.method.upper(),
                        status_code=resp.status_code,
                        response_body=resp_body,
                        request_params=req.params,
                        ttl_days=req.cache_ttl_days,
                    )

                return {
                    "platform": req.platform,
                    "path": req.path,
                    "method": req.method,
                    "source": "live",
                    "status_code": resp.status_code,
                    "latency_ms": latency_ms,
                    "response": resp_body,
                }
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"API call failed: {exc}")

    if not connector:
        raise HTTPException(status_code=404, detail=f"Connector '{req.platform}' not found")

    # CW PSA — route through cwm_gateway for correct auth + content-type
    if req.platform == "connectwise_psa":
        from app.services.cwm_gateway import api_request as cwm_api_request

        path = req.path if req.path.startswith("/") else f"/{req.path}"
        start = time.monotonic()
        try:
            result = await cwm_api_request(req.method.upper(), path, params=req.params, data=req.body)
            latency_ms = int((time.monotonic() - start) * 1000)

            async with factory() as session:
                repo = ApiDiscoveryRepository(session)
                await repo.cache_response(
                    platform=req.platform, endpoint_path=req.path,
                    method=req.method.upper(), status_code=200,
                    response_body=result, request_params=req.params,
                    ttl_days=req.cache_ttl_days,
                )

            return {
                "platform": req.platform, "path": req.path, "method": req.method,
                "source": "live", "status_code": 200, "latency_ms": latency_ms,
                "response": result,
            }
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text[:500])
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"CWM API call failed: {exc}")

    # Build the full URL
    base = ""
    if hasattr(connector, "_base_url"):
        base = connector._base_url()
    if not base:
        raise HTTPException(status_code=400, detail=f"Cannot determine base URL for '{req.platform}'")

    # Asio uses a sub-path for API calls
    if req.platform == "asio" and hasattr(connector, "_api_base"):
        base = connector._api_base()
        url = f"{base}{req.path}"
    else:
        url = f"{base}{req.path}"

    # Authenticate
    try:
        await connector.authenticate()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Authentication failed: {exc}")

    # Build auth headers — prefer the connector's own _auth_headers() method
    headers: dict[str, str] = {}
    if hasattr(connector, "_auth_headers") and callable(connector._auth_headers):
        headers = connector._auth_headers()
    elif hasattr(connector, "_access_token") and connector._access_token:
        headers["Authorization"] = f"Bearer {connector._access_token}"
    elif hasattr(connector, "_settings"):
        s = connector._settings
        if hasattr(s, "s1_api_token") and s.s1_api_token:
            headers["Authorization"] = f"ApiToken {s.s1_api_token}"

    # Execute
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, verify=False) as client:  # noqa: S501
            if req.method.upper() == "GET":
                resp = await client.get(url, headers=headers, params=req.params)
            elif req.method.upper() == "POST":
                resp = await client.post(url, headers=headers, json=req.body, params=req.params)
            elif req.method.upper() == "PUT":
                resp = await client.put(url, headers=headers, json=req.body, params=req.params)
            elif req.method.upper() == "PATCH":
                resp = await client.patch(url, headers=headers, json=req.body, params=req.params)
            elif req.method.upper() == "DELETE":
                resp = await client.delete(url, headers=headers, params=req.params)
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported method: {req.method}")

            latency_ms = int((time.monotonic() - start) * 1000)

            try:
                resp_body = resp.json()
            except Exception:  # noqa: BLE001
                resp_body = {"raw": resp.text[:5000]}

            # Cache the response
            async with factory() as session:
                repo = ApiDiscoveryRepository(session)
                await repo.cache_response(
                    platform=req.platform,
                    endpoint_path=req.path,
                    method=req.method.upper(),
                    status_code=resp.status_code,
                    response_body=resp_body,
                    request_params=req.params,
                    ttl_days=req.cache_ttl_days,
                )

            return {
                "platform": req.platform,
                "path": req.path,
                "method": req.method,
                "source": "live",
                "status_code": resp.status_code,
                "latency_ms": latency_ms,
                "response": resp_body,
            }
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"API call failed: {exc.response.text[:500]}",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"API call failed: {exc}")


@router.get("/cache")
async def list_cache(platform: str | None = None) -> list[dict[str, Any]]:
    """List all cached API responses, optionally filtered by platform."""
    factory = get_session_factory()
    async with factory() as session:
        repo = ApiDiscoveryRepository(session)
        entries = await repo.list_cached_responses(platform)
        return [
            {
                "id": e.id,
                "platform": e.platform,
                "endpoint_path": e.endpoint_path,
                "method": e.method,
                "status_code": e.status_code,
                "cached_at": e.cached_at.isoformat() if e.cached_at else None,
                "expires_at": e.expires_at.isoformat() if e.expires_at else None,
            }
            for e in entries
        ]


@router.delete("/cache/expired")
async def clear_expired_cache() -> dict[str, int]:
    """Remove all expired cache entries."""
    factory = get_session_factory()
    async with factory() as session:
        repo = ApiDiscoveryRepository(session)
        removed = await repo.clear_expired_cache()
        return {"removed": removed}


# ---------------------------------------------------------------------------
# Platform Swagger UI routes  (mounted at root — no /api/discovery prefix)
# ---------------------------------------------------------------------------

platform_docs_router = APIRouter(tags=["platform-docs"])

_SWAGGER_UI_HTML = """\
<!DOCTYPE html>
<html>
<head>
  <title>{title} — API Docs</title>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" type="text/css" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
  <style>
    body {{ margin: 0; }}
    .topbar {{ display: none; }}
    .info .title small {{ display: none; }}
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({{
      url: "{spec_url}",
      dom_id: "#swagger-ui",
      presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
      layout: "BaseLayout",
      deepLinking: true,
      displayRequestDuration: true,
      filter: true,
    }});
  </script>
</body>
</html>
"""

_PLATFORM_INDEX_HTML = """\
<!DOCTYPE html>
<html>
<head>
  <title>Platform API Docs</title>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            max-width: 860px; margin: 40px auto; padding: 0 20px; color: #333; }}
    h1 {{ font-size: 1.6rem; margin-bottom: 4px; }}
    p.sub {{ color: #666; margin-top: 0; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 24px; }}
    th {{ text-align: left; padding: 8px 12px; background: #f5f5f5;
          border-bottom: 2px solid #ddd; font-size: .85rem; text-transform: uppercase; }}
    td {{ padding: 10px 12px; border-bottom: 1px solid #eee; vertical-align: top; }}
    tr:hover td {{ background: #fafafa; }}
    a {{ color: #0066cc; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px;
              font-size: .75rem; background: #e8f4fd; color: #0066cc; }}
    .own {{ background: #e8fde8; color: #1a7a1a; }}
  </style>
</head>
<body>
  <h1>📡 Platform API Docs</h1>
  <p class="sub">Swagger UI for every scanned OpenAPI spec. Use these to explore what each platform can do.</p>
  <table>
    <thead><tr>
      <th>Platform</th><th>Title</th><th>Version</th><th>Endpoints</th><th>Docs</th>
    </tr></thead>
    <tbody>
      {rows}
    </tbody>
  </table>
  <p style="margin-top:32px;color:#999;font-size:.8rem">
    Also available: <a href="/docs">/docs</a> — this app's own API &nbsp;|&nbsp;
    <a href="/api/discovery/specs">/api/discovery/specs</a> — raw spec list
  </p>
</body>
</html>
"""


@platform_docs_router.get(
    "/api/discovery/specs/{platform}/openapi.json",
    include_in_schema=False,
)
async def get_platform_openapi_json(platform: str) -> JSONResponse:
    """Return the stored OpenAPI spec JSON for *platform* (for Swagger UI)."""
    factory = get_session_factory()
    async with factory() as session:
        repo = ApiDiscoveryRepository(session)
        spec = await repo.get_spec_by_platform(platform)
        if not spec or not spec.spec_json:
            raise HTTPException(
                status_code=404,
                detail=f"No spec stored for platform '{platform}'. Run POST /api/discovery/scan first.",
            )
        return JSONResponse(content=spec.spec_json)


@platform_docs_router.get("/docs/platforms", include_in_schema=False)
async def platform_docs_index() -> HTMLResponse:
    """HTML index listing all scanned platforms with links to their Swagger UI."""
    factory = get_session_factory()
    async with factory() as session:
        repo = ApiDiscoveryRepository(session)
        specs = await repo.list_specs()

    rows_html = ""
    for s in sorted(specs, key=lambda x: x.platform):
        badge_cls = "badge own" if s.platform == _SELF_PLATFORM else "badge"
        rows_html += (
            f"<tr>"
            f"<td><span class='{badge_cls}'>{s.platform}</span></td>"
            f"<td>{s.title or ''}</td>"
            f"<td>{s.version or ''}</td>"
            f"<td>{s.endpoint_count or 0}</td>"
            f"<td><a href='/docs/{s.platform}'>Open Docs →</a></td>"
            f"</tr>\n"
        )
    if not rows_html:
        rows_html = "<tr><td colspan='5' style='color:#999'>No specs scanned yet. Run POST /api/discovery/scan.</td></tr>"

    html = _PLATFORM_INDEX_HTML.format(rows=rows_html)
    return HTMLResponse(content=html)


@platform_docs_router.get("/docs/{platform}", include_in_schema=False)
async def platform_swagger_ui(platform: str) -> HTMLResponse:
    """Serve a Swagger UI page for the given platform's stored OpenAPI spec."""
    # Avoid intercepting FastAPI's own oauth2-redirect page
    if platform == "oauth2-redirect":
        raise HTTPException(status_code=404, detail="Not found")
    factory = get_session_factory()
    async with factory() as session:
        repo = ApiDiscoveryRepository(session)
        spec = await repo.get_spec_by_platform(platform)
        if not spec:
            raise HTTPException(
                status_code=404,
                detail=f"No spec stored for platform '{platform}'. Run POST /api/discovery/scan first.",
            )

    title = spec.title or platform
    spec_url = f"/api/discovery/specs/{platform}/openapi.json"
    html = _SWAGGER_UI_HTML.format(title=title, spec_url=spec_url)
    return HTMLResponse(content=html)

