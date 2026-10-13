"""Connector health-check and management API router."""

import time
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import AbstractPlatformConnector
from app.db.repository import ConnectorConfigRepository, get_session_factory
from app.services.connector_registry import (
    PLATFORM_DEFINITIONS,
    build_connector,
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


class ConnectorUpdateRequest(BaseModel):
    """Request body for updating connector credentials."""

    credentials: dict[str, str]
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
async def list_connectors(
    session: AsyncSession = Depends(_get_session),
) -> list[dict[str, Any]]:
    """Return registered connectors with config status and saved configs."""
    repo = ConnectorConfigRepository(session)
    saved_configs = await repo.list_all()
    saved_map = {c.platform: c for c in saved_configs}

    results = []
    for c in _connectors:
        missing = c.check_config()
        saved = saved_map.get(c.platform_name)
        results.append({
            "platform": c.platform_name,
            "configured": len(missing) == 0,
            "missing_config": missing,
            "saved": saved is not None,
            "enabled": saved.enabled if saved else True,
            # Non-secret config/token facts (URLs, scopes requested/granted) — no credentials.
            **({"diagnostics": c.get_diagnostics()} if hasattr(c, "get_diagnostics") else {}),
        })
    return results


def _require_master_key() -> None:
    from app.auth.crypto import master_key_configured

    if not master_key_configured():
        raise HTTPException(status_code=503, detail=(
            "Saving credentials needs MSP_MASTER_KEY in .env (generate one with "
            "`python -m app.cli gen-master-key`), then restart."))


@router.get("/platforms", response_model=list[dict[str, Any]])
async def list_platforms() -> list[dict[str, Any]]:
    """Return all available platform definitions with their credential fields."""
    return [
        {"platform": key, **value}
        for key, value in PLATFORM_DEFINITIONS.items()
    ]


@router.get("/configs", response_model=list[dict[str, Any]])
async def list_configs(
    session: AsyncSession = Depends(_get_session),
) -> list[dict[str, Any]]:
    """Return all saved connector configurations (secrets redacted)."""
    repo = ConnectorConfigRepository(session)
    configs = await repo.list_all()
    return [
        {
            "id": c.id,
            "platform": c.platform,
            "enabled": c.enabled,
            "credentials": redact_credentials(c.credentials, c.platform),
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }
        for c in configs
    ]


@router.post("/register", response_model=dict[str, Any])
async def register_connector(
    body: ConnectorRegisterRequest,
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Register a new connector — save credentials, optionally test, and activate."""
    if body.platform not in PLATFORM_DEFINITIONS:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {body.platform}")

    # Build a connector instance to validate and optionally test
    try:
        connector = build_connector(body.platform, body.credentials)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Validate required fields are present
    missing = connector.check_config()
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required fields: {', '.join(missing)}",
        )

    # Optionally test connectivity
    test_result = None
    if body.test_connection:
        test_result = await _test_connector(connector)
        if test_result["status"] == "error":
            await _close_connector(connector)
            return {
                "status": "test_failed",
                "platform": body.platform,
                "test_result": test_result,
                "saved": False,
            }

    # Save to database (encrypted — needs MSP_MASTER_KEY)
    _require_master_key()
    repo = ConnectorConfigRepository(session)
    await repo.upsert(body.platform, body.credentials, enabled=True)

    # Close old connector's persistent client, then replace in active list
    global _connectors
    for old in _connectors:
        if old.platform_name == body.platform:
            await _close_connector(old)
    _connectors = [c for c in _connectors if c.platform_name != body.platform]
    _connectors.append(connector)

    log.info("connector.registered", platform=body.platform)
    return {
        "status": "registered",
        "platform": body.platform,
        "test_result": test_result,
        "saved": True,
    }


@router.put("/configs/{platform}", response_model=dict[str, Any])
async def update_connector_config(
    platform: str,
    body: ConnectorUpdateRequest,
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Update credentials for an existing connector."""
    repo = ConnectorConfigRepository(session)
    existing = await repo.get_by_platform(platform)
    if not existing:
        raise HTTPException(status_code=404, detail="Connector config not found")

    _require_master_key()
    await repo.upsert(platform, body.credentials, enabled=body.enabled)

    # Close old connector's persistent client, then rebuild and replace
    global _connectors
    for old in _connectors:
        if old.platform_name == platform:
            await _close_connector(old)
    if body.enabled:
        connector = build_connector(platform, body.credentials)
        _connectors = [c for c in _connectors if c.platform_name != platform]
        _connectors.append(connector)
    else:
        _connectors = [c for c in _connectors if c.platform_name != platform]

    log.info("connector.updated", platform=platform, enabled=body.enabled)
    return {"status": "updated", "platform": platform, "enabled": body.enabled}


@router.delete("/configs/{platform}", response_model=dict[str, Any])
async def delete_connector_config(
    platform: str,
    session: AsyncSession = Depends(_get_session),
) -> dict[str, Any]:
    """Remove a saved connector configuration and deactivate it."""
    repo = ConnectorConfigRepository(session)
    deleted = await repo.delete(platform)
    if not deleted:
        raise HTTPException(status_code=404, detail="Connector config not found")

    # Close old connector's persistent client, then remove from active list
    global _connectors
    for old in _connectors:
        if old.platform_name == platform:
            await _close_connector(old)
    _connectors = [c for c in _connectors if c.platform_name != platform]

    log.info("connector.removed", platform=platform)
    return {"status": "removed", "platform": platform}


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
