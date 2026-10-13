"""Database access layer — parameterised queries only, no raw SQL string concatenation."""

import structlog
from typing import Any
from uuid import uuid4

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.schema import (
    Base,
    ApiEndpoint,
    ApiResponseCache,
    ApiSpec,
    AsioLastContactImport,
    AvAssignmentCache,
    BreakdownCompany,
    BreakdownSnapshot,
    CompanyAlias,
    CompanyMapping,
    CompanyMatch,
    CompanyOverride,
    ConnectorConfig,
    ConnectorEvent,
    DeviceAvStatus,
    DeviceMatch,
    DeviceOverride,
    PlatformDevice,
    ReconciledDevice,
    SyncRun,
)
from app.services.company_matcher import CompanyMatchResult
from app.services.device_matcher import DeviceMatchResult


def _make_engine(database_url: str | None = None):  # type: ignore[no-untyped-def]
    url = database_url or get_settings().database_url
    return create_async_engine(url, echo=False)


def _make_session_factory(database_url: str | None = None) -> async_sessionmaker[AsyncSession]:
    engine = _make_engine(database_url)
    return async_sessionmaker(engine, expire_on_commit=False)


# Module-level default session factory (overridden in tests)
_default_factory: async_sessionmaker[AsyncSession] | None = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the module-level session factory, initialising it if needed."""
    global _default_factory
    if _default_factory is None:
        _default_factory = _make_session_factory()
    return _default_factory


async def init_db(database_url: str | None = None) -> None:
    """Bring the database schema up to date (Alembic migrations — see ``app/db/migrations.py``)."""
    from app.db.migrations import upgrade

    engine = _make_engine(database_url)
    try:
        await upgrade(engine)
    finally:
        await engine.dispose()


class OverrideRepository:
    """CRUD operations for company and device overrides."""

    def __init__(self, session: AsyncSession) -> None:
        """Initialise with an injected async SQLAlchemy session."""
        self._session = session

    # ---- Company overrides ----

    async def create_company_override(
        self,
        platform_a: str,
        platform_id_a: str,
        platform_b: str,
        platform_id_b: str,
        note: str | None = None,
    ) -> CompanyOverride:
        """Persist a new company override and return it."""
        obj = CompanyOverride(
            id=str(uuid4()),
            platform_a=platform_a,
            platform_id_a=platform_id_a,
            platform_b=platform_b,
            platform_id_b=platform_id_b,
            note=note,
        )
        self._session.add(obj)
        await self._session.commit()
        await self._session.refresh(obj)
        return obj

    async def list_company_overrides(self) -> list[CompanyOverride]:
        """Return all company overrides."""
        result = await self._session.execute(select(CompanyOverride))
        return list(result.scalars().all())

    # ---- Device overrides ----

    async def create_device_override(
        self,
        platform_a: str,
        platform_id_a: str,
        platform_b: str,
        platform_id_b: str,
        note: str | None = None,
    ) -> DeviceOverride:
        """Persist a new device override and return it."""
        obj = DeviceOverride(
            id=str(uuid4()),
            platform_a=platform_a,
            platform_id_a=platform_id_a,
            platform_b=platform_b,
            platform_id_b=platform_id_b,
            note=note,
        )
        self._session.add(obj)
        await self._session.commit()
        await self._session.refresh(obj)
        return obj

    async def list_device_overrides(self) -> list[DeviceOverride]:
        """Return all device overrides."""
        result = await self._session.execute(select(DeviceOverride))
        return list(result.scalars().all())


class CompanyAliasRepository:
    """CRUD operations for company name aliases (merge mappings)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_alias(
        self,
        alias_name: str,
        canonical_name: str,
        note: str | None = None,
    ) -> CompanyAlias:
        """Create a new alias mapping.  Upserts if alias_name already exists."""
        from sqlalchemy import delete as sa_delete

        # Remove any existing alias for this name
        await self._session.execute(
            sa_delete(CompanyAlias).where(CompanyAlias.alias_name == alias_name)
        )
        obj = CompanyAlias(
            id=str(uuid4()),
            alias_name=alias_name,
            canonical_name=canonical_name,
            note=note,
        )
        self._session.add(obj)
        await self._session.commit()
        await self._session.refresh(obj)
        return obj

    async def list_aliases(self) -> list[CompanyAlias]:
        """Return all company aliases."""
        result = await self._session.execute(
            select(CompanyAlias).order_by(CompanyAlias.canonical_name)
        )
        return list(result.scalars().all())

    async def get_alias_map(self) -> dict[str, str]:
        """Return a dict mapping alias_name → canonical_name for fast lookups."""
        aliases = await self.list_aliases()
        return {a.alias_name: a.canonical_name for a in aliases}

    async def delete_alias(self, alias_id: str) -> bool:
        """Delete an alias by ID.  Returns True if a row was removed."""
        from sqlalchemy import delete as sa_delete

        result = await self._session.execute(
            sa_delete(CompanyAlias).where(CompanyAlias.id == alias_id)
        )
        await self._session.commit()
        return result.rowcount > 0  # type: ignore[union-attr]


class ReconciliationRepository:
    """CRUD operations for storing reconciliation match results."""

    def __init__(self, session: AsyncSession) -> None:
        """Initialise with an injected async SQLAlchemy session."""
        self._session = session

    async def clear_company_matches(self) -> None:
        """Remove all existing company match records."""
        from sqlalchemy import delete

        await self._session.execute(delete(CompanyMatch))
        await self._session.commit()

    async def save_company_matches(
        self, matches: list[CompanyMatchResult], run_id: str | None = None
    ) -> None:
        """Persist company match results, optionally tagged with a run_id."""
        for match in matches:
            obj = CompanyMatch(
                id=str(uuid4()),
                run_id=run_id,
                platform_a=match.source.platform,
                platform_id_a=match.source.platform_id,
                name_a=match.source.name,
                platform_b=match.target.platform if match.target else None,
                platform_id_b=match.target.platform_id if match.target else None,
                name_b=match.target.name if match.target else None,
                score=match.score,
                status=match.status.value,
            )
            self._session.add(obj)
        await self._session.commit()

    async def list_company_matches(self) -> list[CompanyMatch]:
        """Return all stored company matches."""
        result = await self._session.execute(select(CompanyMatch))
        return list(result.scalars().all())

    async def clear_device_matches(self) -> None:
        """Remove all existing device match records."""
        from sqlalchemy import delete

        await self._session.execute(delete(DeviceMatch))
        await self._session.commit()

    async def save_device_matches(
        self, matches: list[DeviceMatchResult], run_id: str | None = None
    ) -> None:
        """Persist device match results, optionally tagged with a run_id."""
        for match in matches:
            obj = DeviceMatch(
                id=str(uuid4()),
                run_id=run_id,
                platform_a=match.source.platform,
                platform_id_a=match.source.platform_id,
                hostname_a=match.source.hostname,
                platform_b=match.target.platform if match.target else None,
                platform_id_b=match.target.platform_id if match.target else None,
                hostname_b=match.target.hostname if match.target else None,
                score=match.score,
                status=match.status.value,
                reasons=match.reasons,
            )
            self._session.add(obj)
        await self._session.commit()

    async def list_device_matches(self) -> list[DeviceMatch]:
        """Return all stored device matches."""
        result = await self._session.execute(select(DeviceMatch))
        return list(result.scalars().all())


class SyncRunRepository:
    """CRUD operations for sync runs and connector events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_run(self, run_id: str) -> SyncRun:
        """Create a new sync run record."""
        obj = SyncRun(id=run_id, status="running")
        self._session.add(obj)
        await self._session.commit()
        await self._session.refresh(obj)
        return obj

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        company_match_count: int,
        device_match_count: int,
        error_count: int = 0,
        errors: list[str] | None = None,
        warning_count: int = 0,
        warnings: list[str] | None = None,
    ) -> None:
        """Mark a sync run as finished with summary counts."""
        from datetime import UTC, datetime

        result = await self._session.execute(select(SyncRun).where(SyncRun.id == run_id))
        run = result.scalar_one()
        run.status = status
        run.finished_at = datetime.now(UTC)
        run.company_match_count = company_match_count
        run.device_match_count = device_match_count
        run.error_count = error_count
        run.errors = errors  # type: ignore[assignment]
        run.warning_count = warning_count
        run.warnings = warnings  # type: ignore[assignment]
        await self._session.commit()

    async def add_connector_event(
        self,
        run_id: str,
        connector: str,
        event_type: str,
        message: str = "",
        company_count: int = 0,
        device_count: int = 0,
        latency_ms: int = 0,
    ) -> ConnectorEvent:
        """Record a connector-level event within a run."""
        obj = ConnectorEvent(
            id=str(uuid4()),
            run_id=run_id,
            connector=connector,
            event_type=event_type,
            message=message,
            company_count=company_count,
            device_count=device_count,
            latency_ms=latency_ms,
        )
        self._session.add(obj)
        await self._session.commit()
        return obj

    async def list_runs(self, limit: int = 50) -> list[SyncRun]:
        """Return recent sync runs ordered by start time descending."""
        result = await self._session.execute(
            select(SyncRun).order_by(SyncRun.started_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def get_run(self, run_id: str) -> SyncRun | None:
        """Return a single sync run by ID."""
        result = await self._session.execute(select(SyncRun).where(SyncRun.id == run_id))
        return result.scalar_one_or_none()

    async def get_run_events(self, run_id: str) -> list[ConnectorEvent]:
        """Return all connector events for a given run."""
        result = await self._session.execute(
            select(ConnectorEvent)
            .where(ConnectorEvent.run_id == run_id)
            .order_by(ConnectorEvent.created_at)
        )
        return list(result.scalars().all())


class ConnectorConfigRepository:
    """CRUD operations for connector configurations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self, platform: str, credentials: dict, enabled: bool = True
    ) -> ConnectorConfig:
        """Create or update a connector configuration."""
        result = await self._session.execute(
            select(ConnectorConfig).where(ConnectorConfig.platform == platform)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.credentials = credentials  # type: ignore[assignment]
            existing.enabled = enabled
            await self._session.commit()
            await self._session.refresh(existing)
            return existing

        obj = ConnectorConfig(
            id=str(uuid4()),
            platform=platform,
            credentials=credentials,  # type: ignore[arg-type]
            enabled=enabled,
        )
        self._session.add(obj)
        await self._session.commit()
        await self._session.refresh(obj)
        return obj

    async def list_all(self) -> list[ConnectorConfig]:
        """Return all saved connector configurations."""
        result = await self._session.execute(
            select(ConnectorConfig).order_by(ConnectorConfig.platform)
        )
        return list(result.scalars().all())

    async def get_by_platform(self, platform: str) -> ConnectorConfig | None:
        """Return a single connector config by platform name."""
        result = await self._session.execute(
            select(ConnectorConfig).where(ConnectorConfig.platform == platform)
        )
        return result.scalar_one_or_none()

    async def delete(self, platform: str) -> bool:
        """Delete a connector config. Returns True if deleted."""
        result = await self._session.execute(
            select(ConnectorConfig).where(ConnectorConfig.platform == platform)
        )
        obj = result.scalar_one_or_none()
        if obj:
            await self._session.delete(obj)
            await self._session.commit()
            return True
        return False


class ReconciledDeviceRepository:
    """CRUD operations for the unified reconciled device records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_all(self, records: list[ReconciledDevice]) -> int:
        """Delete all existing records and insert new ones.  Returns count."""
        from sqlalchemy import delete as sa_delete

        await self._session.execute(sa_delete(ReconciledDevice))
        for rec in records:
            self._session.add(rec)
        await self._session.commit()
        return len(records)

    async def list_all(self) -> list[ReconciledDevice]:
        """Return all reconciled devices ordered by company, hostname."""
        result = await self._session.execute(
            select(ReconciledDevice).order_by(
                ReconciledDevice.company_name,
                ReconciledDevice.hostname,
            )
        )
        return list(result.scalars().all())


class ApiDiscoveryRepository:
    """CRUD operations for API schema discovery — specs, endpoints, cached responses."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- API Specs ----

    async def upsert_spec(
        self,
        platform: str,
        title: str,
        version: str,
        swagger_url: str,
        spec_json: dict,
        endpoint_count: int,
    ) -> ApiSpec:
        """Create or update an API specification for a platform."""
        from datetime import UTC, datetime

        result = await self._session.execute(
            select(ApiSpec).where(ApiSpec.platform == platform)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.title = title
            existing.version = version
            existing.swagger_url = swagger_url
            existing.spec_json = spec_json  # type: ignore[assignment]
            existing.endpoint_count = endpoint_count
            existing.fetched_at = datetime.now(UTC)
            await self._session.commit()
            await self._session.refresh(existing)
            return existing

        obj = ApiSpec(
            id=str(uuid4()),
            platform=platform,
            title=title,
            version=version,
            swagger_url=swagger_url,
            spec_json=spec_json,  # type: ignore[arg-type]
            endpoint_count=endpoint_count,
        )
        self._session.add(obj)
        await self._session.commit()
        await self._session.refresh(obj)
        return obj

    async def list_specs(self) -> list[ApiSpec]:
        """Return all stored API specs ordered by platform."""
        result = await self._session.execute(
            select(ApiSpec).order_by(ApiSpec.platform)
        )
        return list(result.scalars().all())

    async def get_spec_by_platform(self, platform: str) -> ApiSpec | None:
        """Return a single API spec by platform name."""
        result = await self._session.execute(
            select(ApiSpec).where(ApiSpec.platform == platform)
        )
        return result.scalar_one_or_none()

    # ---- API Endpoints ----

    async def replace_endpoints(self, platform: str, endpoints: list[dict]) -> int:
        """Delete all endpoints for a platform and insert new ones.  Returns count."""
        from sqlalchemy import delete as sa_delete

        await self._session.execute(
            sa_delete(ApiEndpoint).where(ApiEndpoint.platform == platform)
        )
        for ep in endpoints:
            obj = ApiEndpoint(
                id=str(uuid4()),
                platform=platform,
                path=ep["path"],
                method=ep["method"],
                summary=ep.get("summary", ""),
                description=ep.get("description", ""),
                tags=ep.get("tags", []),  # type: ignore[arg-type]
                parameters=ep.get("parameters", []),  # type: ignore[arg-type]
                request_body_schema=ep.get("request_body_schema"),  # type: ignore[arg-type]
                response_schema=ep.get("response_schema"),  # type: ignore[arg-type]
            )
            self._session.add(obj)
        await self._session.commit()
        return len(endpoints)

    async def list_endpoints(self, platform: str) -> list[ApiEndpoint]:
        """Return all endpoints for a platform ordered by path."""
        result = await self._session.execute(
            select(ApiEndpoint)
            .where(ApiEndpoint.platform == platform)
            .order_by(ApiEndpoint.path, ApiEndpoint.method)
        )
        return list(result.scalars().all())

    async def search_endpoints(self, query: str, limit: int = 100) -> list[ApiEndpoint]:
        """Ranked search over every platform's endpoints: path, summary, description, tags and parameter names
        (same engine as connection search — synonyms, prefixes, typos)."""
        from app.connections.search import Index

        rows = list((await self._session.execute(select(ApiEndpoint))).scalars().all())
        records = [{"row": ep, "summary": ep.summary, "path": ep.path, "tags": ep.tags or [],
                    "description": (ep.description or "")[:1500], "platform": ep.platform,
                    "params": [p.get("name") for p in ep.parameters or [] if isinstance(p, dict)],
                    "read_only": ep.method.upper() in ("GET", "HEAD", "OPTIONS")} for ep in rows]
        index = Index(records, fields={"title": ["summary"], "target": ["path"], "tags": ["tags"],
                                       "params": ["params"], "description": ["description"],
                                       "connection": ["platform"]})
        return [rec["row"] for _, rec, _ in index.search(query, limit=limit)]

    # ---- Response Cache ----

    async def cache_response(
        self,
        platform: str,
        endpoint_path: str,
        method: str,
        status_code: int,
        response_body: dict | None,
        response_headers: dict | None = None,
        request_params: dict | None = None,
        ttl_days: int = 365,
    ) -> ApiResponseCache | None:
        """Store a cached API response with a configurable TTL. Only GET responses are ever cached."""
        from datetime import UTC, datetime, timedelta

        if method.upper() != "GET":
            return None
        from sqlalchemy import delete as sa_delete

        # Remove any existing cache for this exact call
        await self._session.execute(
            sa_delete(ApiResponseCache).where(
                (ApiResponseCache.platform == platform)
                & (ApiResponseCache.endpoint_path == endpoint_path)
                & (ApiResponseCache.method == method.upper())
            )
        )
        obj = ApiResponseCache(
            id=str(uuid4()),
            platform=platform,
            endpoint_path=endpoint_path,
            method=method.upper(),
            request_params=request_params,  # type: ignore[arg-type]
            status_code=status_code,
            response_body=response_body,  # type: ignore[arg-type]
            response_headers=response_headers,  # type: ignore[arg-type]
            cached_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=ttl_days),
        )
        self._session.add(obj)
        await self._session.commit()
        await self._session.refresh(obj)
        return obj

    async def get_cached_response(
        self, platform: str, endpoint_path: str, method: str
    ) -> ApiResponseCache | None:
        """Return a cached response if it exists and has not expired. Writes are never served from cache."""
        from datetime import UTC, datetime

        if method.upper() != "GET":
            return None
        result = await self._session.execute(
            select(ApiResponseCache).where(
                (ApiResponseCache.platform == platform)
                & (ApiResponseCache.endpoint_path == endpoint_path)
                & (ApiResponseCache.method == method.upper())
            )
        )
        cached = result.scalar_one_or_none()
        if cached and cached.expires_at and cached.expires_at < datetime.now(UTC):
            return None  # expired
        return cached

    async def list_cached_responses(self, platform: str | None = None) -> list[ApiResponseCache]:
        """Return all cached responses, optionally filtered by platform."""
        stmt = select(ApiResponseCache).order_by(
            ApiResponseCache.platform, ApiResponseCache.cached_at.desc()
        )
        if platform:
            stmt = stmt.where(ApiResponseCache.platform == platform)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def clear_expired_cache(self) -> int:
        """Remove all expired cache entries.  Returns number removed."""
        from datetime import UTC, datetime
        from sqlalchemy import delete as sa_delete

        result = await self._session.execute(
            sa_delete(ApiResponseCache).where(
                ApiResponseCache.expires_at < datetime.now(UTC)
            )
        )
        await self._session.commit()
        return result.rowcount or 0


log = structlog.get_logger(__name__)


class PlatformDeviceRepository:
    """Bulk operations for the platform_devices table.

    This is the source-of-truth store for every device seen from every
    platform.  Designed for full-table upsert on each scan cycle.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_upsert(self, devices: list[dict]) -> dict[str, int]:
        """Upsert a batch of device dicts into platform_devices.

        Each dict must contain at least ``platform`` and ``platform_id``.
        Returns ``{"inserted": n, "updated": m}``.
        """
        from datetime import UTC, datetime
        from sqlalchemy import delete as sa_delete

        inserted = 0
        updated = 0
        now = datetime.now(UTC)

        for dev in devices:
            platform = dev["platform"]
            platform_id = dev["platform_id"]

            result = await self._session.execute(
                select(PlatformDevice).where(
                    (PlatformDevice.platform == platform)
                    & (PlatformDevice.platform_id == platform_id)
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                existing.endpoint_id = dev.get("endpoint_id", "")
                existing.hostname = dev.get("hostname", "")
                existing.hostname_norm = dev.get("hostname_norm", "")
                existing.company_raw = dev.get("company_raw", "")
                existing.company_canonical = dev.get("company_canonical", "")
                existing.company_platform_id = dev.get("company_platform_id", "")
                existing.site_name = dev.get("site_name", "")
                existing.serial_number = dev.get("serial_number", "")
                existing.mac_addresses = dev.get("mac_addresses", "")
                existing.raw_json = dev.get("raw_json")
                existing.last_seen_at = now
                updated += 1
            else:
                obj = PlatformDevice(
                    id=str(uuid4()),
                    platform=platform,
                    platform_id=platform_id,
                    endpoint_id=dev.get("endpoint_id", ""),
                    hostname=dev.get("hostname", ""),
                    hostname_norm=dev.get("hostname_norm", ""),
                    company_raw=dev.get("company_raw", ""),
                    company_canonical=dev.get("company_canonical", ""),
                    company_platform_id=dev.get("company_platform_id", ""),
                    site_name=dev.get("site_name", ""),
                    serial_number=dev.get("serial_number", ""),
                    mac_addresses=dev.get("mac_addresses", ""),
                    raw_json=dev.get("raw_json"),
                    first_seen_at=now,
                    last_seen_at=now,
                )
                self._session.add(obj)
                inserted += 1

        await self._session.commit()
        return {"inserted": inserted, "updated": updated}

    async def get_stored_endpoint_ids(self, platform: str) -> dict[str, str]:
        """Return {endpoint_id: company_canonical} for all devices on a platform."""
        result = await self._session.execute(
            select(PlatformDevice.endpoint_id, PlatformDevice.company_canonical).where(
                (PlatformDevice.platform == platform)
                & (PlatformDevice.endpoint_id != "")
            )
        )
        return {row[0]: row[1] for row in result.all()}

    async def get_stored_hostnames(self, platform: str) -> dict[str, str]:
        """Return {hostname_norm: company_canonical} for all devices on a platform."""
        result = await self._session.execute(
            select(PlatformDevice.hostname_norm, PlatformDevice.company_canonical).where(
                (PlatformDevice.platform == platform)
                & (PlatformDevice.hostname_norm != "")
            )
        )
        return {row[0]: row[1] for row in result.all()}

    async def list_by_company(self, company_canonical: str) -> list[PlatformDevice]:
        """Return all devices for a canonical company name across all platforms."""
        result = await self._session.execute(
            select(PlatformDevice).where(
                PlatformDevice.company_canonical == company_canonical
            ).order_by(PlatformDevice.platform, PlatformDevice.hostname)
        )
        return list(result.scalars().all())

    async def list_all_by_platform(self, platform: str) -> list[PlatformDevice]:
        """Return all devices for a specific platform."""
        result = await self._session.execute(
            select(PlatformDevice).where(
                PlatformDevice.platform == platform
            ).order_by(PlatformDevice.hostname)
        )
        return list(result.scalars().all())

    async def list_all(self) -> list[PlatformDevice]:
        """Return all stored platform devices."""
        result = await self._session.execute(
            select(PlatformDevice).order_by(
                PlatformDevice.company_canonical, PlatformDevice.platform, PlatformDevice.hostname
            )
        )
        return list(result.scalars().all())

    async def get_scan_stats(self) -> dict[str, int]:
        """Return device count per platform."""
        from sqlalchemy import func

        result = await self._session.execute(
            select(PlatformDevice.platform, func.count(PlatformDevice.id)).group_by(
                PlatformDevice.platform
            )
        )
        return {row[0]: row[1] for row in result.all()}

    async def mark_stale(self, platform: str, active_platform_ids: set[str]) -> int:
        """Remove devices for a platform that were not in the latest scan."""
        from sqlalchemy import delete as sa_delete

        if not active_platform_ids:
            return 0
        result = await self._session.execute(
            sa_delete(PlatformDevice).where(
                (PlatformDevice.platform == platform)
                & (PlatformDevice.platform_id.notin_(active_platform_ids))
            )
        )
        await self._session.commit()
        return result.rowcount or 0


class AsioLastContactRepository:
    """CRUD operations for CSV-provided Asio last-contact timestamps."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_upsert(self, rows: list[dict[str, str]], source_filename: str = "") -> dict[str, int]:
        """Insert or update imported Asio last-contact rows."""
        from datetime import UTC, datetime

        inserted = 0
        updated = 0
        now = datetime.now(UTC)

        for row in rows:
            endpoint_id = row.get("endpoint_id", "")
            platform_id = row.get("platform_id", "")
            hostname_norm = row.get("hostname_norm", "")

            stmt = select(AsioLastContactImport)
            if endpoint_id:
                stmt = stmt.where(AsioLastContactImport.endpoint_id == endpoint_id)
            elif platform_id:
                stmt = stmt.where(AsioLastContactImport.platform_id == platform_id)
            else:
                stmt = stmt.where(AsioLastContactImport.hostname_norm == hostname_norm)

            result = await self._session.execute(stmt)
            existing = result.scalars().first()

            if existing:
                existing.platform_id = platform_id or existing.platform_id
                existing.hostname = row.get("hostname", existing.hostname)
                existing.hostname_norm = hostname_norm or existing.hostname_norm
                existing.company_name = row.get("company_name", existing.company_name)
                existing.site_name = row.get("site_name", existing.site_name)
                existing.last_contact = row["last_contact"]
                existing.source_filename = source_filename
                existing.imported_at = now
                updated += 1
            else:
                self._session.add(AsioLastContactImport(
                    id=str(uuid4()),
                    endpoint_id=endpoint_id,
                    platform_id=platform_id,
                    hostname=row.get("hostname", ""),
                    hostname_norm=hostname_norm,
                    company_name=row.get("company_name", ""),
                    site_name=row.get("site_name", ""),
                    last_contact=row["last_contact"],
                    source_filename=source_filename,
                    imported_at=now,
                ))
                inserted += 1

        await self._session.commit()
        return {"inserted": inserted, "updated": updated}

    async def get_lookups(self) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
        """Return lookup maps by endpoint ID, platform ID, company+hostname, and hostname."""
        result = await self._session.execute(select(AsioLastContactImport))
        by_endpoint_id: dict[str, str] = {}
        by_platform_id: dict[str, str] = {}
        by_company_hostname: dict[str, str] = {}
        by_hostname: dict[str, str] = {}
        for row in result.scalars().all():
            if row.endpoint_id:
                by_endpoint_id[row.endpoint_id.lower()] = row.last_contact
            if row.platform_id:
                by_platform_id[row.platform_id.lower()] = row.last_contact
            if row.company_name and row.hostname_norm:
                by_company_hostname[f"{row.company_name.lower()}|{row.hostname_norm.lower()}"] = row.last_contact
            if row.hostname_norm:
                by_hostname[row.hostname_norm.lower()] = row.last_contact
        return by_endpoint_id, by_platform_id, by_company_hostname, by_hostname


class CompanyMappingRepository:
    """CRUD operations for the company_mappings table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_upsert(self, mappings: list[dict]) -> int:
        """Upsert a batch of company mapping dicts.

        Each dict: ``{platform, raw_name, normalized_name, canonical_name, alias_source}``.
        Returns count of upserted rows.
        """
        from datetime import UTC, datetime

        count = 0
        now = datetime.now(UTC)
        for m in mappings:
            result = await self._session.execute(
                select(CompanyMapping).where(
                    (CompanyMapping.platform == m["platform"])
                    & (CompanyMapping.raw_name == m["raw_name"])
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.normalized_name = m["normalized_name"]
                existing.canonical_name = m["canonical_name"]
                existing.alias_source = m.get("alias_source", "auto")
                existing.updated_at = now
            else:
                obj = CompanyMapping(
                    id=str(uuid4()),
                    platform=m["platform"],
                    raw_name=m["raw_name"],
                    normalized_name=m["normalized_name"],
                    canonical_name=m["canonical_name"],
                    alias_source=m.get("alias_source", "auto"),
                    created_at=now,
                    updated_at=now,
                )
                self._session.add(obj)
            count += 1

        await self._session.commit()
        return count

    async def get_mapping(self) -> dict[str, str]:
        """Return {normalized_name: canonical_name} for all mappings."""
        result = await self._session.execute(select(CompanyMapping))
        mapping: dict[str, str] = {}
        for row in result.scalars().all():
            mapping[row.normalized_name] = row.canonical_name
        return mapping

    async def get_canonical_for(self, platform: str, raw_name: str) -> str | None:
        """Return canonical name for a specific platform+raw_name, or None."""
        result = await self._session.execute(
            select(CompanyMapping.canonical_name).where(
                (CompanyMapping.platform == platform)
                & (CompanyMapping.raw_name == raw_name)
            )
        )
        row = result.scalar_one_or_none()
        return row if row else None

    async def list_all(self) -> list[CompanyMapping]:
        """Return all company mappings."""
        result = await self._session.execute(
            select(CompanyMapping).order_by(
                CompanyMapping.platform, CompanyMapping.canonical_name
            )
        )
        return list(result.scalars().all())


class DeviceAvStatusRepository:
    """CRUD operations for the device_av_status table.

    Stores per-device AV detection results from multiple sources:
    ScreenConnect commands, SentinelOne installed-apps, ESET PROTECT,
    PSA agreements, manual/CSV uploads.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_upsert(self, statuses: list[dict]) -> dict[str, int]:
        """Upsert a batch of AV status dicts.

        Each dict should contain at minimum:
        ``hostname_norm``, ``av_vendor``, ``detection_source``.

        Returns ``{"inserted": n, "updated": m}``.
        """
        from datetime import UTC, datetime

        inserted = 0
        updated = 0
        now = datetime.now(UTC)

        for s in statuses:
            result = await self._session.execute(
                select(DeviceAvStatus).where(
                    (DeviceAvStatus.hostname_norm == s["hostname_norm"])
                    & (DeviceAvStatus.av_vendor == s["av_vendor"])
                    & (DeviceAvStatus.detection_source == s["detection_source"])
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                existing.hostname = s.get("hostname", existing.hostname)
                existing.endpoint_id = s.get("endpoint_id", existing.endpoint_id)
                existing.company_canonical = s.get("company_canonical", existing.company_canonical)
                existing.av_product = s.get("av_product", existing.av_product)
                existing.av_version = s.get("av_version", existing.av_version)
                existing.av_status = s.get("av_status", existing.av_status)
                existing.av_enabled = s.get("av_enabled", existing.av_enabled)
                existing.av_up_to_date = s.get("av_up_to_date", existing.av_up_to_date)
                existing.detected_at = s.get("detected_at", now)
                existing.updated_at = now
                updated += 1
            else:
                obj = DeviceAvStatus(
                    id=str(uuid4()),
                    hostname=s.get("hostname", ""),
                    hostname_norm=s["hostname_norm"],
                    endpoint_id=s.get("endpoint_id", ""),
                    company_canonical=s.get("company_canonical", ""),
                    av_vendor=s["av_vendor"],
                    av_product=s.get("av_product", ""),
                    av_version=s.get("av_version", ""),
                    av_status=s.get("av_status", "installed"),
                    av_enabled=s.get("av_enabled"),
                    av_up_to_date=s.get("av_up_to_date"),
                    detection_source=s["detection_source"],
                    detected_at=s.get("detected_at", now),
                    created_at=now,
                    updated_at=now,
                )
                self._session.add(obj)
                inserted += 1

        await self._session.commit()
        return {"inserted": inserted, "updated": updated}

    async def get_by_hostname(self, hostname_norm: str) -> list[DeviceAvStatus]:
        """Return all AV status records for a normalised hostname."""
        result = await self._session.execute(
            select(DeviceAvStatus).where(
                DeviceAvStatus.hostname_norm == hostname_norm
            ).order_by(DeviceAvStatus.av_vendor)
        )
        return list(result.scalars().all())

    async def get_by_endpoint_id(self, endpoint_id: str) -> list[DeviceAvStatus]:
        """Return all AV status records for an endpoint ID."""
        result = await self._session.execute(
            select(DeviceAvStatus).where(
                DeviceAvStatus.endpoint_id == endpoint_id
            ).order_by(DeviceAvStatus.av_vendor)
        )
        return list(result.scalars().all())

    async def list_all(self) -> list[DeviceAvStatus]:
        """Return all stored AV status records."""
        result = await self._session.execute(
            select(DeviceAvStatus).order_by(
                DeviceAvStatus.company_canonical,
                DeviceAvStatus.hostname_norm,
                DeviceAvStatus.av_vendor,
            )
        )
        return list(result.scalars().all())

    async def list_by_company(self, company_canonical: str) -> list[DeviceAvStatus]:
        """Return all AV status records for a canonical company."""
        result = await self._session.execute(
            select(DeviceAvStatus).where(
                DeviceAvStatus.company_canonical == company_canonical
            ).order_by(DeviceAvStatus.hostname_norm, DeviceAvStatus.av_vendor)
        )
        return list(result.scalars().all())

    async def get_av_lookup(self) -> dict[str, list[dict[str, Any]]]:
        """Return {hostname_norm: [{av_vendor, av_product, av_status, ...}]} for all devices.

        Designed to be called once during reconciliation to build an in-memory
        lookup table that can be joined with the device inventory.
        """
        from typing import Any as _Any

        result = await self._session.execute(
            select(DeviceAvStatus).order_by(
                DeviceAvStatus.hostname_norm, DeviceAvStatus.av_vendor
            )
        )
        lookup: dict[str, list[dict[str, _Any]]] = {}
        for row in result.scalars().all():
            entry = {
                "av_vendor": row.av_vendor,
                "av_product": row.av_product,
                "av_version": row.av_version,
                "av_status": row.av_status,
                "av_enabled": row.av_enabled,
                "av_up_to_date": row.av_up_to_date,
                "detection_source": row.detection_source,
                "detected_at": row.detected_at.isoformat() if row.detected_at else None,
            }
            lookup.setdefault(row.hostname_norm, []).append(entry)
        return lookup

    async def get_stats(self) -> dict[str, Any]:
        """Return summary stats: total devices with AV, by vendor, by source."""
        from sqlalchemy import func
        from typing import Any as _Any

        # Count by vendor
        vendor_result = await self._session.execute(
            select(DeviceAvStatus.av_vendor, func.count(DeviceAvStatus.id)).group_by(
                DeviceAvStatus.av_vendor
            )
        )
        by_vendor = {row[0]: row[1] for row in vendor_result.all()}

        # Count by source
        source_result = await self._session.execute(
            select(DeviceAvStatus.detection_source, func.count(DeviceAvStatus.id)).group_by(
                DeviceAvStatus.detection_source
            )
        )
        by_source = {row[0]: row[1] for row in source_result.all()}

        # Unique hostnames
        unique_result = await self._session.execute(
            select(func.count(func.distinct(DeviceAvStatus.hostname_norm)))
        )
        unique_devices = unique_result.scalar() or 0

        return {
            "total_records": sum(by_vendor.values()),
            "unique_devices": unique_devices,
            "by_vendor": by_vendor,
            "by_source": by_source,
        }


class BreakdownCacheRepository:
    """Read/write the pre-computed enriched breakdown for the Breakdown tab.

    ``breakdown_companies`` stores one fully-enriched JSON row per company
    so ``GET /api/companies/reconciled`` never recomputes or calls live APIs.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[BreakdownCompany]:
        """Return all cached company rows ordered by company name."""
        result = await self._session.execute(
            select(BreakdownCompany).order_by(BreakdownCompany.company_name)
        )
        return list(result.scalars().all())

    async def computed_at(self) -> Any:
        """Return the most-recent ``computed_at`` timestamp across all rows, or None."""
        result = await self._session.execute(
            select(BreakdownCompany.computed_at)
            .order_by(BreakdownCompany.computed_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def replace_all(self, companies: list[dict], run_id: str) -> int:
        """Atomically replace the entire breakdown cache.

        Deletes all existing rows then bulk-inserts the new set.
        Returns the number of companies written.
        """
        from datetime import UTC, datetime
        from sqlalchemy import delete

        await self._session.execute(delete(BreakdownCompany))

        now = datetime.now(UTC)
        for c in companies:
            name = c.get("company", "")
            if not name:
                continue
            obj = BreakdownCompany(
                company_name=name,
                data=c,
                matched_count=c.get("matched_count", 0),
                mismatched_count=c.get("mismatched_count", 0),
                assigned_av=c.get("assigned_av", "none"),
                psa_present=bool(c.get("psa_present")),
                computed_at=now,
                run_id=run_id,
            )
            self._session.add(obj)

        return len(companies)


class BreakdownSnapshotRepository:
    """Record and diff sequential breakdown snapshots for change tracking."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest(self) -> BreakdownSnapshot | None:
        """Return the most recent snapshot."""
        result = await self._session.execute(
            select(BreakdownSnapshot)
            .order_by(BreakdownSnapshot.snapshot_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_recent(self, limit: int = 30) -> list[BreakdownSnapshot]:
        result = await self._session.execute(
            select(BreakdownSnapshot)
            .order_by(BreakdownSnapshot.snapshot_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def save(
        self,
        run_id: str,
        companies: list[dict],
        previous: "BreakdownSnapshot | None" = None,
    ) -> BreakdownSnapshot:
        """Compute + save a new snapshot, including a diff from the previous one."""
        from datetime import UTC, datetime

        matched = sum(c.get("matched_count", 0) for c in companies)
        mismatched = sum(c.get("mismatched_count", 0) for c in companies)
        curr_names = sorted({c["company"] for c in companies if c.get("company")})

        changes: dict[str, Any] = {}
        if previous and previous.company_names:
            prev_names = set(previous.company_names)
            curr_set = set(curr_names)
            added = sorted(curr_set - prev_names)
            removed = sorted(prev_names - curr_set)
            if added or removed or matched != previous.matched_count or mismatched != previous.mismatched_count:
                changes = {
                    "companies_added": added,
                    "companies_removed": removed,
                    "matched_delta": matched - (previous.matched_count or 0),
                    "mismatched_delta": mismatched - (previous.mismatched_count or 0),
                }

        snapshot = BreakdownSnapshot(
            id=str(uuid4()),
            run_id=run_id,
            snapshot_at=datetime.now(UTC),
            company_count=len(companies),
            matched_count=matched,
            mismatched_count=mismatched,
            total_unique=matched + mismatched,
            company_names=curr_names,
            changes=changes if changes else None,
        )
        self._session.add(snapshot)
        return snapshot


class AvAssignmentCacheRepository:
    """Persist and retrieve PSA AV-assignment data between reconcile runs.

    The PSA ``get_company_av_map()`` call is expensive (~5–10s).  We store
    the result here so ``run_fast_reconciliation`` can enrich the breakdown
    without any live API calls.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_map(self) -> dict[str, str]:
        """Return {company_name: assigned_av} for all stored entries."""
        result = await self._session.execute(select(AvAssignmentCache))
        return {row.company_name: row.assigned_av for row in result.scalars().all()}

    async def replace_all(self, av_map: dict[str, str]) -> int:
        """Replace the entire AV assignment cache atomically."""
        from datetime import UTC, datetime
        from sqlalchemy import delete

        await self._session.execute(delete(AvAssignmentCache))
        now = datetime.now(UTC)
        for company_name, assigned_av in av_map.items():
            self._session.add(AvAssignmentCache(
                company_name=company_name,
                assigned_av=assigned_av,
                refreshed_at=now,
            ))
        return len(av_map)
