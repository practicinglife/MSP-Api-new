"""FastAPI application entry point.

``create_app()`` builds the **core** — connections to any API, one MCP server per connection plus the hub,
local copies, search, sign-in, keys / OAuth, write modes, change approvals, audit — and adds the packs named in
``MSP_PACKS`` (see ``app/packs``). ``app`` is the instance uvicorn serves.
"""

import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

from app import packs
from app.db.repository import get_session_factory, init_db

log = structlog.get_logger(__name__)

# App pages revalidate on every load so an upgrade is visible immediately (no stale cached UI).
_NO_CACHE = {"Cache-Control": "no-cache"}


async def _security_startup_checks() -> None:
    """Encrypt any plaintext stored credentials and warn loudly about missing security configuration."""
    from app.api.setup import needs_setup, setup_code
    from app.auth.crypto import master_key_configured, reencrypt_plaintext_rows
    from app.org_profile import external_base_url

    if master_key_configured():
        try:
            if converted := await reencrypt_plaintext_rows():
                log.info("security.credentials_encrypted", rows=converted)
        except Exception as exc:  # noqa: BLE001
            log.error("security.credential_encryption_failed", error=str(exc))
    else:
        log.warning("security.no_master_key",
                    hint="Saving credentials and first-run setup are disabled until MSP_MASTER_KEY is set "
                         "(python -m app.cli gen-master-key)")
    try:
        first_run = await needs_setup()
    except Exception:  # noqa: BLE001
        first_run = False
    if first_run:
        code = setup_code()
        log.warning("security.no_sign_in_method",
                    hint="Nobody can sign in yet. Open /setup on this server from your network"
                         + (f" (e.g. {external_base_url()}/setup) and enter setup code {code}" if code else
                            " after setting MSP_MASTER_KEY")
                         + ", or run `python -m app.cli create-user admin --role admin`")


def _lifespan(active: list[str]):  # type: ignore[no-untyped-def]
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        log.info("app.startup", packs=active or "none (core only)")
        await init_db()
        await _security_startup_checks()
        for name in active:
            try:
                await packs.module(name).startup(app)
            except Exception as exc:  # noqa: BLE001
                log.error("pack.startup_failed", pack=name, error=str(exc))

        # Local copy: run each connection sync when its interval is due (MSP_CONNECTION_SYNC=off disables)
        from app.connections.sync import start_sync_loop

        sync_task = start_sync_loop()
        yield
        if sync_task is not None:
            sync_task.cancel()
        for name in active:
            try:
                await packs.module(name).shutdown(app)
            except Exception as exc:  # noqa: BLE001
                log.error("pack.shutdown_failed", pack=name, error=str(exc))
        log.info("app.shutdown")

    return lifespan


def custom_generate_unique_id(route: APIRoute) -> str:
    """Stable operation ids (``<tag>_<function_name>``): readable names in the OpenAPI spec."""
    tag = route.tags[0] if route.tags else "api"
    tag = re.sub(r"[^a-zA-Z0-9]+", "_", str(tag)).strip("_").lower()
    return f"{tag}_{route.name}"


def create_app(enabled_packs: list[str] | None = None) -> FastAPI:
    """Build the app with the core plus ``enabled_packs`` (default: ``MSP_PACKS``)."""
    from app import runtime

    runtime.apply_settings()  # address and sign-in chosen in Get started, where .env didn't set them
    active = packs.configured() if enabled_packs is None else [p for p in enabled_packs if p in packs.AVAILABLE]
    packs.set_active(active)

    application = FastAPI(
        title="API Tools",
        version="1.0.0",
        lifespan=_lifespan(active),
        generate_unique_id_function=custom_generate_unique_id,
    )

    # Authentication + authorization + audit on every request; refuses internet traffic that doesn't come
    # through the nginx edge (e.g. tunnels).
    from app.security import NetworkBoundaryMiddleware

    application.add_middleware(NetworkBoundaryMiddleware)

    # ── Core ──
    from app.api.admin_security import router as admin_security_router
    from app.api.auth_local import router as auth_local_router
    from app.api.auth_m365 import router as auth_m365_router
    from app.api.changes import router as changes_router
    from app.api.connection_settings import router as connection_settings_router
    from app.api.connections import router as connections_router
    from app.api.connections import search_router as connections_search_router
    from app.api.external_auth import router as external_auth_router
    from app.api.mcp_servers import router as mcp_servers_router
    from app.api.setup import router as setup_router
    from app.api.secure_setup import router as secure_setup_router

    # Sign-in (OIDC / Microsoft 365 + local accounts), connection keys + OAuth for MCP clients, security admin
    application.include_router(auth_m365_router)
    application.include_router(auth_local_router)
    application.include_router(external_auth_router)
    application.include_router(admin_security_router)
    application.include_router(setup_router)
    application.include_router(secure_setup_router)  # Get started → Secure it (Admin)
    # Any-API connections (OpenAPI, Postman, cURL, SOAP, GraphQL, OData, SQL, MongoDB, MCP), search, local copy
    application.include_router(connections_router)
    application.include_router(connections_search_router)
    application.include_router(connection_settings_router)  # export / import (Approver)
    # Each connection as its own MCP server (/mcp/{slug}), hand-picked groups, and the hub (/mcp)
    application.include_router(mcp_servers_router)
    # Change requests: approval-mode writes (anyone proposes → an Editor submits, an Approver approves)
    application.include_router(changes_router)

    @application.get("/connections", tags=["dashboard"], include_in_schema=False)
    async def connections_page() -> FileResponse:
        """Connections: add APIs, search every operation, try them, keep local copies, set write modes."""
        return FileResponse("app/static/connections.html", headers=_NO_CACHE)

    @application.get("/api/packs", tags=["admin"])
    async def packs_info() -> dict[str, object]:
        """Which packs are on, what each adds, and its navigation links."""
        return packs.describe()

    @application.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # ── Packs ──
    for name in active:
        packs.module(name).register(application)

    if "/" not in {getattr(r, "path", None) for r in application.routes}:
        @application.get("/", include_in_schema=False)
        async def home() -> RedirectResponse:
            return RedirectResponse("/connections", status_code=302)

    application.mount("/static", StaticFiles(directory="app/static"), name="static")
    if "msp" in active:
        # Saved PDF/JSON/HTML reports (MSP pack) for download
        from pathlib import Path

        reports = Path(__file__).resolve().parent.parent / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        application.mount("/reports", StaticFiles(directory=str(reports)), name="reports")
    return application


# MCP is served only by authenticated servers: one per connection plus the hub (app/api/mcp_servers.py) and,
# with the MSP pack, the curated /msp/mcp (app/api/org_mcp.py).
app = create_app()

__all__ = ["app", "create_app", "get_session_factory"]
