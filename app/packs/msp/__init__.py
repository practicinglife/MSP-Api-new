"""MSP pack: ConnectWise PSA, Asio, ScreenConnect, SentinelOne and ESET, kept in this server's own database.

Turn on with ``MSP_PACKS=msp``. Everything it adds lives behind this module:

* **Vendor connectors** (``app/connectors/``) loaded from ``.env`` or saved credentials, and their scheduled
  syncs into Postgres: companies, devices, agreements, the client registry, daily snapshots, AV status.
* **Cross-tool features** reading that local data: reconciliation, client registry and naming, AV gaps,
  discrepancies, helpdesk triage, onboarding / offboarding, tickets, reports, remediation.
* **The MSP dashboard** (``/``), the API explorer, ``/msp/mcp`` (curated tools + org skills, recipes and
  playbooks) and the MSP change commands.
* **Background jobs**: nightly smart reconcile + AV sweep, SentinelOne alert triage.

The code itself stays in ``app/api``, ``app/services`` and ``app/connectors``; this module is the single place
that decides whether any of it is reachable.

**Scopes.** Connection keys and OAuth tokens carry scopes ``<platform|*>:<read|write>``. The pack mixes data
from every vendor in one database, so it is all-or-nothing: a credential may use it (``/msp/mcp`` tool calls,
every route of the routers below, MSP change commands, saved ``/reports``) only with ``*:read``; pack routes that
need the Editor role also need ``*:write``. A key scoped to single platforms (``petstore:read``, even
``connectwise_psa:read``) gets 403 / an MCP tool error. The one exception is the raw vendor proxy
``/api/proxy/<platform>/…``, which checks that platform's own scope (``<platform>:read``, ``<platform>:write``).
People signing in always get ``*:read`` (Editors and up ``*:write``), so they are unaffected. Enforced by
``app/security.py`` (``scope_applies``), ``app/api/org_mcp.py``, ``app/api/changes.py`` and ``app/api/proxy.py``.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from fastapi import FastAPI

log = structlog.get_logger(__name__)

TITLE = "MSP pack"
DESCRIPTION = ("ConnectWise PSA, Asio, ScreenConnect, SentinelOne and ESET: synced into the local database, "
               "client registry, reconciliation, AV gaps, discrepancies, helpdesk, onboarding, /msp/mcp.")
NAV = [{"href": "/", "label": "MSP dashboard"}, {"href": "/library", "label": "Workflow library"}]

# (module, attribute) of every router the pack adds, in the order they were always included.
_ROUTERS = [
    ("app.api.company_matching", "router"), ("app.api.companies", "router"), ("app.api.devices", "router"), ("app.api.overrides", "router"),
    ("app.api.runs", "router"), ("app.api.connector_health", "router"), ("app.api.gap_analysis", "router"),
    ("app.api.gap_analysis", "gap_analysis_router"), ("app.api.db_query", "router"),
    ("app.api.actions", "router"), ("app.api.api_discovery", "router"),
    ("app.api.api_discovery", "platform_docs_router"), ("app.api.remediation", "router"),
    ("app.api.tickets", "router"), ("app.api.proxy", "router"), ("app.api.proxy", "catalog_router"),
    ("app.api.reports", "router"), ("app.api.agent", "router"), ("app.api.fingerprints", "router"),
    ("app.api.clients", "router"),
    # Company-wide MCP server (/msp/mcp) + skill/plugin distribution (/msp/plugin, /msp/connect, /library)
    ("app.api.org_mcp", "router"),
    ("app.api.onboarding", "router"),
    # Antivirus gap review (/av-review, /api/av/*), discrepancies, helpdesk triage
    ("app.api.av_review", "router"), ("app.api.discrepancies", "router"), ("app.api.helpdesk", "router"),
    ("app.packs.msp.routes", "router"),
]


# Paths the scope rule (see the module docstring) covers, filled on first use.
_PATHS: list[re.Pattern[str]] = []
_EXTRA_PATHS = [re.compile(r"^/reports(/.*)?$")]  # saved reports (a StaticFiles mount, not a router)
_OWN_SCOPES = re.compile(r"^/api/proxy/")  # checks <platform>:read / <platform>:write itself (app/api/proxy.py)


def scope_applies(path: str) -> bool:
    """Does the pack's ``*:read`` / ``*:write`` rule apply to ``path``: served by the active pack, not the proxy?"""
    from app import packs

    if not packs.is_active("msp") or _OWN_SCOPES.match(path):
        return False
    if not _PATHS:
        import importlib

        for mod, attr in _ROUTERS:
            try:
                router = getattr(importlib.import_module(mod), attr)
            except Exception:  # noqa: BLE001 — a disabled router serves nothing
                continue
            _PATHS.extend(r.path_regex for r in router.routes if getattr(r, "path_regex", None) is not None)
        _PATHS.extend(_EXTRA_PATHS)
    return any(p.match(path) for p in _PATHS)


def scope_denied(principal: Any, action: str = "read") -> str | None:
    """Why ``principal``'s scopes don't cover the pack for ``action`` ("read" / "write"), or None if they do."""
    if principal.allows("*", action):
        return None
    return f"This credential's scopes don't include the MSP tools (needs *:{action})"


def register(app: FastAPI) -> None:
    import importlib

    for mod, attr in _ROUTERS:
        try:
            app.include_router(getattr(importlib.import_module(mod), attr))
        except Exception as exc:  # noqa: BLE001 — one broken feature must not take the app down
            log.error("msp.router_disabled", router=f"{mod}.{attr}", error=str(exc))


def default_connectors() -> list[Any]:
    """Vendor connectors configured in .env (only those whose settings pass check_config())."""
    from app.connectors.asio import AsioConnector
    from app.connectors.connectwise_psa import ConnectWisePsaConnector
    from app.connectors.eset import EsetConnector
    from app.connectors.screenconnect import ScreenConnectConnector
    from app.connectors.sentinelone import SentinelOneConnector

    candidates = [ConnectWisePsaConnector(), ScreenConnectConnector(), AsioConnector(), EsetConnector(),
                  SentinelOneConnector()]
    return [c for c in candidates if not c.check_config()]


async def startup(app: FastAPI) -> None:
    from app.api.connector_health import register_connectors
    from app.org_profile import missing_msp_settings
    from app.services import builtin_integrations as builtins

    if missing := missing_msp_settings():
        log.warning("org_profile.msp_settings_missing", missing=missing,
                    hint="MSP features that depend on these are off until they are set in .env")

    # Built-in tools are connections (kind "builtin"): tools set in .env or the old connector_configs table get
    # a row once, then the live connectors are built from the rows (saved credentials first, then .env).
    connectors = default_connectors()
    try:
        await builtins.adopt_existing()
        connectors = await builtins.load_live_connectors()
        log.info("connectors.ready", total=len(connectors), platforms=sorted(c.platform_name for c in connectors))
    except Exception as exc:  # noqa: BLE001 — fall back to .env only
        log.warning("connectors.db_load_error", error=str(exc))
    register_connectors(connectors)
    try:  # each built-in's operations (from the vendor's API definition), without delaying startup
        from sqlalchemy import select

        from app.db.repository import get_session_factory
        from app.db.schema import Connection

        async with get_session_factory()() as db:
            pending = [r.slug for r in (await db.execute(select(Connection).where(
                Connection.kind == builtins.KIND, Connection.operation_count == 0))).scalars()]
        builtins.import_in_background(pending)
    except Exception as exc:  # noqa: BLE001
        log.warning("builtin.operations_schedule_failed", error=str(exc))

    # The API explorer starts with this app's own endpoints
    try:
        from app.api.api_discovery import seed_self_spec

        await seed_self_spec(app.openapi())
    except Exception as exc:  # noqa: BLE001
        log.warning("startup.self_spec_seed_failed", error=str(exc))

    try:
        from app.api.av_review import start_nightly
        from app.api.helpdesk import start_triage_loop

        start_nightly()
        start_triage_loop()
    except Exception as exc:  # noqa: BLE001
        log.warning("startup.nightly_failed", error=str(exc))


async def shutdown(app: FastAPI) -> None:
    return None
