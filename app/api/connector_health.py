"""Connector health-check and management API router."""

import time
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import AbstractPlatformConnector
from app.db.repository import get_session_factory
from app.services.connector_registry import (
    PLATFORM_DEFINITIONS,
    redact_credentials,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/connectors", tags=["connectors"])

# Active connector instances — set by main.py at startup, updated by register/remove
_connectors: list[AbstractPlatformConnector] = []


def register_connectors(connectors: list[AbstractPlatformConnector]) -> None:
    """Register connector instances for health checking."""
    global _connectors
    _connectors = connectors


def get_connectors() -> list[AbstractPlatformConnector]:
    """Return the current active connector list."""
    return _connectors


async def _get_session() -> AsyncSession:  # type: ignore[return]
    factory = get_session_factory()
    async with factory() as session:
        yield session  # type: ignore[misc]


class ConnectorRegisterRequest(BaseModel):
    """Request body for registering a connector."""

    platform: str
    credentials: dict[str, str]
    test_connection: bool = True
    save: bool = True  # False: only test the credentials


class ConnectorUpdateRequest(BaseModel):
    """Request body for updating connector credentials."""

    credentials: dict[str, str] = {}
    enabled: bool = True


async def _close_connector(connector: AbstractPlatformConnector) -> None:
    """Close a connector's persistent HTTP client if it has one."""
    if hasattr(connector, "close"):
        try:
            await connector.close()
        except Exception:  # noqa: BLE001
            pass


async def _test_connector(connector: AbstractPlatformConnector) -> dict[str, Any]:
    """Run a connectivity test against a single connector.

    First validates configuration, then authenticates and fetches companies.
    """
    # Helper: attach diagnostics if the connector supports them
    def _attach_diagnostics(result: dict[str, Any]) -> dict[str, Any]:
        if hasattr(connector, "get_diagnostics"):
            try:
                result["diagnostics"] = connector.get_diagnostics()
            except Exception:  # noqa: BLE001
                result["diagnostics"] = {"error": "Failed to retrieve diagnostics"}
        return result

    # Check config before making any network calls
    missing = connector.check_config()
    if missing:
        log.warning(
            "connector_health.not_configured",
            platform=connector.platform_name,
            missing=missing,
        )
        return _attach_diagnostics({
            "platform": connector.platform_name,
            "status": "not_configured",
            "latency_ms": 0,
            "companies_found": 0,
            "error": f"Missing configuration: {', '.join(missing)}",
        })

    start = time.monotonic()
    try:
        await connector.authenticate()
        companies = await connector.get_companies()
        latency_ms = int((time.monotonic() - start) * 1000)
        log.info(
            "connector_health.ok",
            platform=connector.platform_name,
            latency_ms=latency_ms,
            companies=len(companies),
        )
        return _attach_diagnostics({
            "platform": connector.platform_name,
            "status": "ok",
            "latency_ms": latency_ms,
            "companies_found": len(companies),
            "error": None,
        })
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.monotonic() - start) * 1000)
        error_msg = str(exc)
        log.error(
            "connector_health.error",
            platform=connector.platform_name,
            error=error_msg,
            error_type=type(exc).__name__,
            latency_ms=latency_ms,
        )
        return _attach_diagnostics({
            "platform": connector.platform_name,
            "status": "error",
            "latency_ms": latency_ms,
            "companies_found": 0,
            "error": error_msg,
            "error_type": type(exc).__name__,
        })


@router.get(
    "",
    response_model=list[dict[str, Any]],
    summary="Connector health: per-platform configured/enabled status",
)
async def list_connectors() -> list[dict[str, Any]]:
    """Return live connectors with config status. Built-in tools are connections (kind "builtin")."""
    from app.services import builtin_integrations as builtins

    state = {c["platform"]: c for c in await builtins.catalog()}
    results = []
    for c in _connectors:
        missing = c.check_config()
        info = state.get(c.platform_name, {})
        results.append({
            "platform": c.platform_name,
            "configured": len(missing) == 0,
            "missing_config": missing,
            "saved": info.get("credentials_from") == "app",
            "credentials_from": info.get("credentials_from", ""),
            "enabled": info.get("enabled", True),
            # Non-secret config/token facts (URLs, scopes requested/granted) — no credentials.
            **({"diagnostics": c.get_diagnostics()} if hasattr(c, "get_diagnostics") else {}),
        })
    return results


@router.get("/catalog", response_model=list[dict[str, Any]])
async def connector_catalog() -> list[dict[str, Any]]:
    """Every built-in tool (management and security), whether it's connected, and where its credentials
    come from ("app" = saved encrypted here, "env" = .env). Secrets are redacted."""
    from app.services import builtin_integrations as builtins

    return await builtins.catalog()


@router.post("/catalog/{platform}/test", response_model=dict[str, Any])
async def test_catalog_entry(platform: str) -> dict[str, Any]:
    """Test a built-in tool with its stored (or .env) credentials — management, security and detail sources."""
    from app.services import builtin_integrations as builtins

    try:
        return await builtins.test(platform)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


def _require_master_key() -> None:
    from app.auth.crypto import master_key_configured

    if not master_key_configured():
        raise HTTPException(status_code=503, detail=(
            "Saving credentials needs MSP_MASTER_KEY in .env (generate one with "
            "`python -m app.cli gen-master-key`), then restart."))


@router.get("/platforms", response_model=list[dict[str, Any]])
async def list_platforms() -> list[dict[str, Any]]:
    """Return all available platform definitions with their credential fields."""
    from app.services.builtin_integrations import ROLES

    return [
        {"platform": key, "role": ROLES.get(key, "other"), **value}
        for key, value in PLATFORM_DEFINITIONS.items()
    ]


@router.get("/configs", response_model=list[dict[str, Any]])
async def list_configs() -> list[dict[str, Any]]:
    """Return built-in tools whose credentials are saved in the app (secrets redacted)."""
    from sqlalchemy import select

    from app.db.schema import Connection

    async with get_session_factory()() as db:
        rows = (await db.execute(select(Connection).where(Connection.kind == "builtin"))).scalars().all()
    return [
        {
            "id": c.id,
            "platform": c.slug,
            "enabled": c.enabled,
            "credentials": redact_credentials(dict(c.secrets), c.slug),
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }
        for c in rows if c.secrets
    ]


def _who() -> str:
    from app.auth.principal import get_current

    p = get_current()
    return p.id if p else ""


@router.post("/register", response_model=dict[str, Any])
async def register_connector(body: ConnectorRegisterRequest) -> dict[str, Any]:
    """Save a built-in tool's credentials (encrypted) and make it live; optionally test first.
    ``save=false`` only tests the credentials."""
    from app.services import builtin_integrations as builtins

    if body.platform not in PLATFORM_DEFINITIONS:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {body.platform}")
    test_result = None
    if body.test_connection or not body.save:
        try:
            test_result = await builtins.test(body.platform, body.credentials)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if test_result["status"] != "ok":
            return {"status": "test_failed", "platform": body.platform, "test_result": test_result, "saved": False}
    if not body.save:
        return {"status": "tested", "platform": body.platform, "test_result": test_result, "saved": False}
    try:
        await builtins.save(body.platform, body.credentials, actor=_who())
    except PermissionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    log.info("connector.registered", platform=body.platform)
    return {"status": "registered", "platform": body.platform, "test_result": test_result, "saved": True}


@router.put("/configs/{platform}", response_model=dict[str, Any])
async def update_connector_config(platform: str, body: ConnectorUpdateRequest) -> dict[str, Any]:
    """Update a built-in tool's credentials, or switch it off and on."""
    from app.services import builtin_integrations as builtins

    if platform not in PLATFORM_DEFINITIONS:
        raise HTTPException(status_code=404, detail="Unknown platform")
    try:
        if body.credentials:
            await builtins.save(platform, body.credentials, enabled=body.enabled, actor=_who())
        else:
            await builtins.set_enabled(platform, body.enabled, actor=_who())
    except PermissionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    log.info("connector.updated", platform=platform, enabled=body.enabled)
    return {"status": "updated", "platform": platform, "enabled": body.enabled}


@router.delete("/configs/{platform}", response_model=dict[str, Any])
async def delete_connector_config(platform: str) -> dict[str, Any]:
    """Forget a built-in tool's saved credentials (a tool still set in .env stays connected from there)."""
    from app.services import builtin_integrations as builtins

    try:
        result = await builtins.remove(platform, actor=_who())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    log.info("connector.removed", platform=platform)
    return {"status": "removed", **result}


@router.post("/test", response_model=list[dict[str, Any]])
async def test_all_connectors() -> list[dict[str, Any]]:
    """Run connectivity tests against all registered connectors."""
    results = []
    for connector in _connectors:
        result = await _test_connector(connector)
        results.append(result)
    return results


@router.post("/test/{platform_name}", response_model=dict[str, Any])
async def test_single_connector(platform_name: str) -> dict[str, Any]:
    """Run a connectivity test against a single connector by platform name."""
    for connector in _connectors:
        if connector.platform_name == platform_name:
            return await _test_connector(connector)
    return {"platform": platform_name, "status": "not_found", "latency_ms": 0, "error": "Unknown platform"}
