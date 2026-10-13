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
"""

from __future__ import annotations

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
    ("app.api.companies", "router"), ("app.api.devices", "router"), ("app.api.overrides", "router"),
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
    from app.db.repository import ConnectorConfigRepository, get_session_factory
    from app.org_profile import missing_msp_settings
    from app.services.connector_registry import build_connector

    if missing := missing_msp_settings():
        log.warning("org_profile.msp_settings_missing", missing=missing,
                    hint="MSP features that depend on these are off until they are set in .env")

    # Connectors from .env, overridden by credentials saved in the database
    connectors = default_connectors()
    try:
        async with get_session_factory()() as session:
            saved = await ConnectorConfigRepository(session).list_all()
        from_db: set[str] = set()
        for cfg in saved:
            if not cfg.enabled:
                continue
            try:
                connector = build_connector(cfg.platform, cfg.credentials)
                connectors = [c for c in connectors if c.platform_name != cfg.platform] + [connector]
                from_db.add(cfg.platform)
                log.info("connector.loaded_from_db", platform=cfg.platform)
            except Exception as exc:  # noqa: BLE001
                log.warning("connector.db_load_failed", platform=cfg.platform, error=str(exc))
        log.info("connectors.ready", total=len(connectors), from_db=len(from_db))
    except Exception as exc:  # noqa: BLE001
        log.warning("connectors.db_load_error", error=str(exc))
    register_connectors(connectors)

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
