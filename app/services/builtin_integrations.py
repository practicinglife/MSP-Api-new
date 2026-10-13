"""Built-in integrations: the MSP pack's vendor tools, kept as ordinary connections.

Each platform (ConnectWise PSA, Asio, ScreenConnect, SentinelOne, ESET, …) is one ``connections`` row with
``kind="builtin"`` and ``slug`` = the platform name, so it is listed on /connections, searchable, callable
(every operation in the vendor's API definition) and its own MCP server — with the same write modes, scopes and
audit as any other connection. Its credentials live in that row's encrypted ``secrets``; ``.env`` still works
and is shown as "from .env" until credentials are saved in the app.

The live connector objects used by reconciliation, AV review, helpdesk … are built from these rows
(:func:`load_live_connectors`) and swapped in place when credentials change.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import delete, select

from app.db.repository import get_session_factory
from app.db.schema import Connection, ConnectorConfig, Operation
from app.services.connector_registry import (
    PLATFORM_DEFINITIONS,
    build_connector,
    redact_credentials,
)

log = structlog.get_logger(__name__)

KIND = "builtin"
# What each tool is for. Management tools normally carry the same company names; security tools are matched to
# those companies (see company matching).
ROLES: dict[str, str] = {
    "connectwise_psa": "management", "asio": "management", "screenconnect": "management",
    "sentinelone": "security", "eset": "security", "avast": "security", "eset_connect": "security",
    "generic_rest": "other",
}
ROLE_LABELS = {"management": "Management", "security": "Security", "other": "Other"}
# Sources that only add detail to devices found elsewhere (no company list): never in the live connector list,
# which reconciliation walks for companies and devices. Features ask for them with detail_connector().
DETAIL_SOURCES = {"eset_connect"}
# Shown first, in this order
ORDER = ["connectwise_psa", "asio", "screenconnect", "sentinelone", "eset", "avast", "eset_connect", "generic_rest"]


def _now() -> datetime:
    return datetime.now(UTC)


def _env_connector(platform: str) -> Any | None:
    """The connector built from ``.env`` when its settings are complete there."""
    from app.connectors.asio import AsioConnector
    from app.connectors.connectwise_psa import ConnectWisePsaConnector
    from app.connectors.eset import EsetConnector
    from app.connectors.eset_connect import EsetConnectConnector
    from app.connectors.screenconnect import ScreenConnectConnector
    from app.connectors.sentinelone import SentinelOneConnector

    classes = {"connectwise_psa": ConnectWisePsaConnector, "screenconnect": ScreenConnectConnector,
               "asio": AsioConnector, "eset": EsetConnector, "sentinelone": SentinelOneConnector,
               "eset_connect": EsetConnectConnector}
    cls = classes.get(platform)
    if cls is None:
        return None
    connector = cls()
    return None if connector.check_config() else connector


def _label(platform: str) -> str:
    return str(PLATFORM_DEFINITIONS.get(platform, {}).get("label") or platform)


def _clean(platform: str, credentials: dict[str, Any]) -> dict[str, str]:
    fields = {f["key"] for f in PLATFORM_DEFINITIONS[platform]["fields"]}
    return {k: str(v).strip() for k, v in credentials.items() if k in fields and v not in (None, "")}


def _secret_keys(platform: str) -> set[str]:
    return {f["key"] for f in PLATFORM_DEFINITIONS.get(platform, {}).get("fields", []) if f.get("type") == "password"}


async def _row(db: Any, platform: str) -> Connection | None:
    return (await db.execute(select(Connection).where(Connection.slug == platform))).scalar_one_or_none()


def _new_row(platform: str, secrets: dict[str, str] | None, actor: str) -> Connection:
    return Connection(
        id=str(uuid4()), slug=platform, name=_label(platform), kind=KIND,
        base_url=(secrets or {}).get("base_url", ""), auth_type="none", secrets=secrets or None,
        settings={"platform": platform, "role": ROLES.get(platform, "other")}, source={"builtin": platform},
        description=f"Built-in {_label(platform)} integration", enabled=True, operation_count=0,
        created_by=actor, created_at=_now())


# ── Live connectors ──────────────────────────────────────────────────────────────────────────────────


async def load_live_connectors() -> list[Any]:
    """Connector objects for every enabled built-in: credentials saved in the app first, then ``.env``."""
    connectors: dict[str, Any] = {}
    for platform in ORDER:
        if platform in DETAIL_SOURCES:
            continue
        env = _env_connector(platform)
        if env is not None:
            connectors[platform] = env
    async with get_session_factory()() as db:
        rows = (await db.execute(select(Connection).where(Connection.kind == KIND))).scalars().all()
    for row in rows:
        platform = (row.settings or {}).get("platform") or row.slug
        if platform in DETAIL_SOURCES:
            continue
        if not row.enabled:
            connectors.pop(platform, None)
            continue
        if row.secrets:
            try:
                connectors[platform] = build_connector(platform, dict(row.secrets))
            except Exception as exc:  # noqa: BLE001 — one bad row must not stop the others
                log.warning("builtin.load_failed", platform=platform, error=str(exc))
    return list(connectors.values())


async def detail_connector(platform: str) -> Any | None:
    """A detail source's connector (credentials saved in the app first, then ``.env``), or None when it isn't
    set up or is switched off."""
    async with get_session_factory()() as db:
        row = await _row(db, platform)
    if row is not None and not row.enabled:
        return None
    if row is not None and row.secrets:
        return build_connector(platform, dict(row.secrets))
    return _env_connector(platform)


async def _swap_live(platform: str) -> None:
    """Rebuild one platform's live connector after its row changed."""
    from app.api import connector_health

    wanted = {c.platform_name: c for c in await load_live_connectors()}
    current = list(connector_health.get_connectors())
    for old in current:
        if old.platform_name == platform:
            await connector_health._close_connector(old)
    others = [c for c in current if c.platform_name != platform]
    connector_health.register_connectors(others + ([wanted[platform]] if platform in wanted else []))


# ── Adoption: .env tools and the old connector_configs table become rows ─────────────────────────────


async def adopt_existing(actor: str = "system") -> list[str]:
    """Create rows for tools configured in ``.env`` or the old ``connector_configs`` table. Idempotent.
    Returns the platforms whose rows were created (their operations still need importing)."""
    created: list[str] = []
    async with get_session_factory()() as db:
        have = {r.slug for r in (await db.execute(select(Connection).where(Connection.kind == KIND))).scalars()}
        legacy = {c.platform: c for c in (await db.execute(select(ConnectorConfig))).scalars()}
        for platform in ORDER:
            if platform in have:
                continue
            old = legacy.get(platform)
            if old is not None:
                row = _new_row(platform, _clean(platform, dict(old.credentials or {})), actor)
                row.enabled = bool(old.enabled)
            elif _env_connector(platform) is not None:
                row = _new_row(platform, None, actor)
            else:
                continue
            db.add(row)
            created.append(platform)
        await db.commit()
    if created:
        log.info("builtin.adopted", platforms=created)
    return created


async def import_operations(platform: str) -> int:
    """(Re)load the platform's operations from its API definition."""
    from app.connections import service

    try:
        result = await service.reimport(platform, actor="system")
        return int(result.get("operations", 0))
    except Exception as exc:  # noqa: BLE001
        log.warning("builtin.operations_import_failed", platform=platform, error=str(exc))
        return 0


def import_in_background(platforms: list[str]) -> None:
    async def _run() -> None:
        for p in platforms:
            n = await import_operations(p)
            log.info("builtin.operations_imported", platform=p, operations=n)

    if platforms:
        asyncio.get_running_loop().create_task(_run())


# ── Catalog, save, remove ────────────────────────────────────────────────────────────────────────────


async def catalog() -> list[dict[str, Any]]:
    """Every built-in tool with its state, for /connections and the Get started wizard."""
    from app.api.connector_health import get_connectors

    live = {c.platform_name for c in get_connectors()}
    async with get_session_factory()() as db:
        rows = {r.slug: r for r in (await db.execute(select(Connection).where(Connection.kind == KIND))).scalars()}
    out = []
    for platform in ORDER:
        d = PLATFORM_DEFINITIONS.get(platform)
        if not d:
            continue
        row = rows.get(platform)
        saved = bool(row and row.secrets)
        env = _env_connector(platform) is not None
        source = "app" if saved else ("env" if env else "")
        enabled = bool(row.enabled) if row else env
        connected = platform in live or (platform in DETAIL_SOURCES and bool(source) and enabled)
        out.append({
            "platform": platform, "label": d["label"], "role": ROLES.get(platform, "other"),
            "role_label": ROLE_LABELS[ROLES.get(platform, "other")], "fields": d["fields"],
            "connected": connected, "enabled": enabled, "detail_source": platform in DETAIL_SOURCES,
            "credentials_from": source, "operations": row.operation_count if row else 0,
            "values": redact_credentials(dict(row.secrets), platform) if saved else {},
        })
    return out


async def test(platform: str, credentials: dict[str, Any] | None = None) -> dict[str, Any]:
    """Try credentials (given, else the stored / .env ones) without saving: sign in and read the company list."""
    from app.api.connector_health import _close_connector, _test_connector, get_connectors

    if platform not in PLATFORM_DEFINITIONS:
        raise ValueError(f"Unknown platform: {platform}")
    if platform in DETAIL_SOURCES:
        connector = build_connector(platform, await _merged(platform, credentials or {})) if credentials             else await detail_connector(platform)
        return await _test_detail_source(platform, connector)
    if credentials:
        merged = await _merged(platform, credentials)
        connector = build_connector(platform, merged)
        try:
            return await _test_connector(connector)
        finally:
            await _close_connector(connector)
    live = next((c for c in get_connectors() if c.platform_name == platform), None)
    if live is None:
        return {"platform": platform, "status": "not_configured", "error": "No credentials yet", "latency_ms": 0,
                "companies_found": 0}
    return await _test_connector(live)


async def _test_detail_source(platform: str, connector: Any | None) -> dict[str, Any]:
    """Sign in and count device groups (detail sources have no company list)."""
    import time

    if connector is None or connector.check_config():
        missing = connector.check_config() if connector is not None else ["credentials"]
        return {"platform": platform, "status": "not_configured", "latency_ms": 0, "companies_found": 0,
                "error": f"Missing configuration: {', '.join(missing)}"}
    start = time.monotonic()
    try:
        if not await connector.authenticate():
            return {"platform": platform, "status": "error", "latency_ms": int((time.monotonic() - start) * 1000),
                    "companies_found": 0,
                    "error": "Sign-in failed: check the username, password and the region in both URLs"}
        groups = await connector.get_device_groups()
    except Exception as exc:  # noqa: BLE001
        return {"platform": platform, "status": "error", "latency_ms": int((time.monotonic() - start) * 1000),
                "companies_found": 0, "error": str(exc)}
    return {"platform": platform, "status": "ok", "latency_ms": int((time.monotonic() - start) * 1000),
            "companies_found": len(groups), "found_label": "device groups", "error": None}


async def _merged(platform: str, credentials: dict[str, Any]) -> dict[str, str]:
    """New values over the saved ones: a blank or redacted secret field keeps what is stored."""
    new = _clean(platform, credentials)
    for k in _secret_keys(platform):
        if new.get(k, "").startswith("***") or set(new.get(k, "")) == {"*"}:
            new.pop(k)
    async with get_session_factory()() as db:
        row = await _row(db, platform)
    return {**(dict(row.secrets) if row and row.secrets else {}), **new}


async def save(platform: str, credentials: dict[str, Any], *, enabled: bool = True, actor: str = "") -> dict[str, Any]:
    """Store credentials (encrypted) and make the tool live. Missing required fields raise ValueError."""
    from app.auth.crypto import master_key_configured

    if platform not in PLATFORM_DEFINITIONS:
        raise ValueError(f"Unknown platform: {platform}")
    merged = await _merged(platform, credentials)
    missing = build_connector(platform, merged).check_config()
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")
    if not master_key_configured():
        raise PermissionError("Saving credentials needs MSP_MASTER_KEY in .env "
                              "(python -m app.cli gen-master-key), then a restart")
    created = False
    async with get_session_factory()() as db:
        row = await _row(db, platform)
        if row is None:
            row = _new_row(platform, merged, actor)
            db.add(row)
            created = True
        elif row.kind != KIND:
            raise ValueError(f"A different connection already uses the name '{platform}'")
        row.secrets, row.enabled = merged, enabled
        row.base_url = merged.get("base_url", row.base_url or "")
        row.updated_at = _now()
        await db.commit()
        needs_ops = created or not row.operation_count
    await _swap_live(platform)
    if needs_ops:
        import_in_background([platform])
    log.info("builtin.saved", platform=platform, enabled=enabled, by=actor)
    return {"platform": platform, "saved": True, "enabled": enabled, "created": created}


async def set_enabled(platform: str, enabled: bool, actor: str = "") -> None:
    async with get_session_factory()() as db:
        row = await _row(db, platform)
        if row is None:
            if not enabled:
                row = _new_row(platform, None, actor)  # remember "off" for a .env tool
                db.add(row)
            else:
                return
        row.enabled = enabled
        await db.commit()
    await _swap_live(platform)


async def remove(platform: str, actor: str = "") -> dict[str, Any]:
    """Forget the saved credentials. A tool still set in ``.env`` stays connected from there."""
    async with get_session_factory()() as db:
        row = await _row(db, platform)
        if row is None or row.kind != KIND:
            raise LookupError(f"No saved settings for {platform}")
        keep_row = _env_connector(platform) is not None
        if keep_row:
            row.secrets, row.enabled = None, True
        else:
            await db.execute(delete(Operation).where(Operation.connection_id == row.id),
                             execution_options={"synchronize_session": False})
            await db.delete(row)
        await db.execute(delete(ConnectorConfig).where(ConnectorConfig.platform == platform))
        await db.commit()
    await _swap_live(platform)
    log.info("builtin.removed", platform=platform, by=actor, still_from_env=keep_row)
    return {"platform": platform, "removed": True, "still_connected_from_env": keep_row}
