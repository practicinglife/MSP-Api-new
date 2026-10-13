"""Reconciliation orchestrator — runs all connectors concurrently and matches records."""

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import structlog

from app.connectors.base import AbstractPlatformConnector
from app.db.repository import (
    PlatformDeviceRepository,
    ReconciliationRepository,
    ReconciledDeviceRepository,
    SyncRunRepository,
    get_session_factory,
)
from app.db.schema import DailySnapshot
from app.db.schema import ReconciledDevice as ReconciledDeviceRow
from app.models.company import CompanyRecord
from app.models.device import DeviceRecord
from app.services.company_matcher import CompanyMatchResult, match_company
from app.services.device_matcher import DeviceMatchResult, match_device

log = structlog.get_logger(__name__)


@dataclass
class ConnectorFetchResult:
    """Result of fetching data from a single connector."""

    platform: str
    companies: list[CompanyRecord] = field(default_factory=list)
    devices: list[DeviceRecord] = field(default_factory=list)
    error: str | None = None
    latency_ms: int = 0


@dataclass
class ReconciliationReport:
    """Summary of a full reconciliation run."""

    run_id: str
    company_matches: list[CompanyMatchResult]
    device_matches: list[DeviceMatchResult]
    errors: list[str]
    connector_results: list[ConnectorFetchResult] = field(default_factory=list)


async def _fetch_platform(
    connector: AbstractPlatformConnector,
) -> ConnectorFetchResult:
    """Fetch companies and devices from one connector with timing."""
    start = time.monotonic()
    try:
        companies = await connector.get_companies()
        devices = await connector.get_devices()
        elapsed = int((time.monotonic() - start) * 1000)
        log.info(
            "reconciliation.connector_success",
            platform=connector.platform_name,
            companies=len(companies),
            devices=len(devices),
            latency_ms=elapsed,
        )
        return ConnectorFetchResult(
            platform=connector.platform_name,
            companies=companies,
            devices=devices,
            latency_ms=elapsed,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.monotonic() - start) * 1000)
        msg = f"{connector.platform_name}: {exc}"
        log.error(
            "reconciliation.connector_error",
            platform=connector.platform_name,
            error=str(exc),
            latency_ms=elapsed,
        )
        return ConnectorFetchResult(
            platform=connector.platform_name,
            error=msg,
            latency_ms=elapsed,
        )


async def _save_sync_run(
    run_id: str,
    *,
    status: str,
    company_match_count: int,
    device_match_count: int,
    error_count: int = 0,
    errors: list[str] | None = None,
) -> None:
    """Persist a completed SyncRun record (fast/smart modes don't use the full run pipeline)."""
    from datetime import UTC, datetime

    factory = get_session_factory()
    async with factory() as session:
        repo = SyncRunRepository(session)
        await repo.create_run(run_id)
        await repo.finish_run(
            run_id,
            status=status,
            company_match_count=company_match_count,
            device_match_count=device_match_count,
            error_count=error_count,
            errors=errors,
        )


async def _ensure_platform_devices_schema() -> None:
    """Ensure required ``platform_devices`` columns exist for live reconciliation.

    This self-heals older databases that were created before newer columns
    (like ``company_platform_id``) were introduced.
    """
    from sqlalchemy import text as sa_text

    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            sa_text(
                "ALTER TABLE platform_devices "
                "ADD COLUMN IF NOT EXISTS company_platform_id VARCHAR(256) NOT NULL DEFAULT ''"
            )
        )
        await session.execute(sa_text("ALTER TABLE platform_devices ADD COLUMN IF NOT EXISTS client_id VARCHAR(36)"))
        await session.commit()

    log.info(
        "reconciliation.schema_checked",
        table="platform_devices",
        ensured_columns=["company_platform_id", "client_id"],
    )


async def _refresh_av_assignment_cache(connectors: list) -> None:
    """Fetch PSA AV assignment map and persist it to ``av_assignment_cache``.

    Called by full and smart reconcile runs so ``run_fast_reconciliation``
    can enrich the breakdown from the DB without live API calls.
    """
    from app.db.repository import AvAssignmentCacheRepository

    psa = next((c for c in connectors if c.platform_name == "connectwise_psa"), None)
    if not psa:
        return
    try:
        raw_av: dict[str, str] = await asyncio.wait_for(
            psa.get_company_av_map(), timeout=90.0
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("refresh_av_cache.failed", error=str(exc))
        return

    factory = get_session_factory()
    async with factory() as session:
        repo = AvAssignmentCacheRepository(session)
        count = await repo.replace_all(raw_av)
        await session.commit()
    log.info("refresh_av_cache.done", companies=count)


async def _take_daily_snapshot(run_id: str) -> None:
    """Snapshot today's per-company breakdown from reconciled_devices.

    Reads the same data the UI breakdown page shows — matched/unmatched counts
    plus per-platform device counts. One row per (date, company).

    Uses INSERT ... ON CONFLICT DO UPDATE so running reconcile multiple times
    per day just refreshes today's row.
    """
    from datetime import date
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.db.schema import PlatformDevice as PlatformDeviceRow

    today = date.today().isoformat()
    factory = get_session_factory()
    async with factory() as session:
        # Per-company matched/unmatched from reconciled_devices
        from sqlalchemy import func, select as sa_select, case
        from app.db.schema import ReconciledDevice as ReconciledDeviceRow

        match_rows = (await session.execute(
            sa_select(
                ReconciledDeviceRow.company_name,
                func.count().label("total"),
                func.sum(case((ReconciledDeviceRow.match_type != "unmatched", 1), else_=0)).label("matched"),
                func.sum(case((ReconciledDeviceRow.match_type == "unmatched",  1), else_=0)).label("unmatched"),
            )
            .where(ReconciledDeviceRow.company_name != "")
            .group_by(ReconciledDeviceRow.company_name)
        )).all()

        if not match_rows:
            log.warning("snapshot.skipped", reason="reconciled_devices empty")
            return

        # Per-company per-platform counts from platform_devices
        plat_rows = (await session.execute(
            sa_select(
                PlatformDeviceRow.company_canonical,
                PlatformDeviceRow.platform,
                func.count().label("cnt"),
            )
            .where(PlatformDeviceRow.company_canonical != "")
            .group_by(PlatformDeviceRow.company_canonical, PlatformDeviceRow.platform)
        )).all()

        # Build platform lookup: {company: {platform: count}}
        plat_lookup: dict[str, dict[str, int]] = {}
        for company, platform, cnt in plat_rows:
            plat_lookup.setdefault(company, {})[platform] = cnt

        count = 0
        for company, total, matched, unmatched in match_rows:
            plats = plat_lookup.get(company, {})
            stmt = pg_insert(DailySnapshot).values(
                id=str(uuid4()),
                snapshot_date=today,
                company_canonical=company,
                total_devices=int(total or 0),
                matched_count=int(matched or 0),
                unmatched_count=int(unmatched or 0),
                asio_count=plats.get("asio", 0),
                psa_count=plats.get("connectwise_psa", 0),
                sc_count=plats.get("screenconnect", 0),
                s1_count=plats.get("sentinelone", 0),
                run_id=run_id,
            ).on_conflict_do_update(
                constraint="uq_snapshot_company_date",
                set_={
                    "total_devices": int(total or 0),
                    "matched_count": int(matched or 0),
                    "unmatched_count": int(unmatched or 0),
                    "asio_count": plats.get("asio", 0),
                    "psa_count": plats.get("connectwise_psa", 0),
                    "sc_count": plats.get("screenconnect", 0),
                    "s1_count": plats.get("sentinelone", 0),
                    "run_id": run_id,
                },
            )
            await session.execute(stmt)
            count += 1

        await session.commit()

    log.info("snapshot.taken", date=today, companies=count, run_id=run_id)


async def _save_breakdown_to_reconciled_devices(
    run_id: str,
    all_devices: list[DeviceRecord],
) -> None:
    """Persist the breakdown from reconcile_from_db into reconciled_devices.

    Uses the same endpoint_id + hostname matching logic as the UI breakdown page,
    so db_* tools and PDF reports show identical data to what the UI shows.
    """
    from app.services.device_identity import reconcile_from_db

    factory = get_session_factory()
    async with factory() as session:
        # Load the freshly-upserted PlatformDevice ORM rows (reconcile_from_db needs ORM objects)
        from sqlalchemy import select as sa_select
        from app.db.schema import PlatformDevice as PlatformDeviceRow
        orm_devices = (await session.execute(sa_select(PlatformDeviceRow))).scalars().all()

        if not orm_devices:
            log.warning("reconciliation.reconciled_devices_skipped", reason="platform_devices empty")
            return

        # Run the same matching logic the UI uses — CPU-bound, so off the event loop
        breakdown: list[dict] = await asyncio.to_thread(reconcile_from_db, orm_devices)

        # Flatten breakdown into ReconciledDevice rows
        rows: list[ReconciledDeviceRow] = []
        for company_data in breakdown:
            company = company_data.get("company", "")
            if not company:
                continue

            for dev in company_data.get("matched", []):
                rows.append(ReconciledDeviceRow(
                    id=str(uuid4()),
                    company_name=company,
                    site_name=dev.get("site_name", "") or "",
                    hostname=dev.get("hostname", "") or "",
                    endpoint_id=dev.get("endpoint_id", "") or "",
                    mac_address=",".join(dev.get("mac_addresses") or dev.get("mac_address", "").split(",") or []),
                    serial_number=dev.get("serial_number", "") or "",
                    platforms=dev.get("platforms", []),
                    match_type="full" if len(dev.get("platforms", [])) >= 3 else "partial",
                ))

            for platform, devs in (company_data.get("mismatched") or {}).items():
                for dev in devs:
                    rows.append(ReconciledDeviceRow(
                        id=str(uuid4()),
                        company_name=company,
                        site_name=dev.get("site_name", "") or "",
                        hostname=dev.get("hostname", "") or "",
                        endpoint_id=dev.get("endpoint_id", "") or "",
                        mac_address=",".join(dev.get("mac_addresses") or dev.get("mac_address", "").split(",") or []),
                        serial_number=dev.get("serial_number", "") or "",
                        platforms=[platform],
                        match_type="unmatched",
                    ))

        repo = ReconciledDeviceRepository(session)
        count = await repo.replace_all(rows)

    log.info(
        "reconciliation.reconciled_devices_saved",
        run_id=run_id,
        total=count,
        full=sum(1 for r in rows if r.match_type == "full"),
        partial=sum(1 for r in rows if r.match_type == "partial"),
        unmatched=sum(1 for r in rows if r.match_type == "unmatched"),
    )


async def run_smart_reconciliation(connectors: list) -> dict[str, Any]:
    """Delta reconcile — fetch only changed devices, reuse DB data for the rest.

    Flow:
    1. Probe each platform: call get_device_ids() concurrently (~3-8s total)
       — returns just {platform_id, company_name, hostname, updated_at}
    2. Compare probed IDs against platform_devices (our DB)
       — new IDs (not in DB), removed IDs (in DB but not on platform)
    3. For platforms with changes: call get_devices_for_ids(new_ids) only
       — full detail fetch for NEW devices only, not the full catalogue
    4. Upsert only changed records into platform_devices
    5. Re-run reconcile_from_db on updated platform_devices
    6. Save reconciled_devices + take snapshot

    Typical runtime: 5-15s (vs 60-90s full reconcile) when <20% of devices changed.
    Falls back to full reconcile if platform probe fails.
    """
    import hashlib
    from datetime import UTC, datetime
    from app.services.device_identity import prepare_device_rows, reconcile_from_db
    from app.services.company_matcher import build_cross_platform_aliases, build_device_cross_reference_aliases
    from app.db.schema import PlatformDevice as PlatformDeviceRow
    from app.db.repository import (
        CompanyAliasRepository, CompanyMappingRepository, PlatformDeviceRepository,
        ReconciledDeviceRepository,
    )
    from app.services.device_identity import prepare_company_mappings

    run_id = str(uuid4())
    log.info("smart_reconciliation.start", run_id=run_id, platforms=len(connectors))

    # Self-heal schema drift on live systems before ORM queries run.
    await _ensure_platform_devices_schema()

    factory = get_session_factory()

    # ── Step 1: Probe all platforms concurrently (ID-only, fast) ──────────────
    # For connectors without a custom get_device_ids() the base class calls
    # get_devices() internally to build the stub list.  We detect this by
    # checking whether the connector overrides the method, and if not we call
    # get_devices() ourselves and cache the result so Step 3 can reuse it
    # without a second round-trip.
    from app.connectors.base import AbstractPlatformConnector as _Base

    _has_custom_probe: dict[str, bool] = {}
    for c in connectors:
        _has_custom_probe[c.platform_name] = (
            type(c).get_device_ids is not _Base.get_device_ids
        )

    # Cache full device lists for connectors that will need them anyway
    _prefetched_devices: dict[str, list[DeviceRecord]] = {}

    async def _safe_probe(connector):
        try:
            if not _has_custom_probe[connector.platform_name]:
                # No custom probe — do the full fetch once and reuse it
                devices = await connector.get_devices()
                _prefetched_devices[connector.platform_name] = devices
                stubs = [
                    {
                        "platform_id":  d.platform_id or "",
                        "company_name": d.company_name or "",
                        "hostname":     d.hostname or "",
                        "updated_at":   "",
                    }
                    for d in devices
                ]
                log.info(
                    "smart_reconcile.probe_prefetch",
                    platform=connector.platform_name,
                    devices=len(devices),
                    stubs_with_id=sum(1 for s in stubs if s["platform_id"]),
                )
                return connector.platform_name, stubs, None
            stubs = await connector.get_device_ids()
            log.info(
                "smart_reconcile.probe_custom",
                platform=connector.platform_name,
                stubs=len(stubs),
                stubs_with_id=sum(1 for s in stubs if s["platform_id"]),
            )
            return connector.platform_name, stubs, None
        except Exception as exc:
            log.warning("smart_reconcile.probe_failed", platform=connector.platform_name, error=str(exc))
            return connector.platform_name, [], str(exc)

    probe_results = await asyncio.gather(*[_safe_probe(c) for c in connectors])

    # ── Step 2: Diff probed IDs against DB ────────────────────────────────────
    async with factory() as session:
        from sqlalchemy import select as sa_select
        db_ids_q = (await session.execute(
            sa_select(PlatformDeviceRow.platform, PlatformDeviceRow.platform_id)
        )).all()

    db_id_set: dict[str, set[str]] = {}
    for plat, pid in db_ids_q:
        db_id_set.setdefault(plat, set()).add(pid)

    # Determine what changed per platform
    platform_new_ids: dict[str, list[str]] = {}    # IDs to fetch full details for
    platform_removed_ids: dict[str, set[str]] = {}  # IDs to remove from DB
    total_new = 0

    for platform, stubs, error in probe_results:
        if error or not stubs:
            # Probe failed — fall back to full fetch for this platform
            log.warning("smart_reconcile.probe_empty", platform=platform, error=error, stubs=len(stubs) if stubs else 0)
            platform_new_ids[platform] = ["__FULL_FETCH__"]
            continue

        probed = {s["platform_id"] for s in stubs if s["platform_id"]}
        if not probed:
            # All stubs had empty platform_id — log and full-fetch
            log.warning("smart_reconcile.probe_no_ids", platform=platform, total_stubs=len(stubs))
            platform_new_ids[platform] = ["__FULL_FETCH__"]
            continue

        existing = db_id_set.get(platform, set())
        new_ids = probed - existing
        removed_ids = existing - probed
        platform_new_ids[platform] = list(new_ids)
        platform_removed_ids[platform] = removed_ids
        total_new += len(new_ids)
        log.info(
            "smart_reconcile.diff",
            platform=platform,
            probed=len(probed),
            existing=len(existing),
            new=len(new_ids),
            removed=len(removed_ids),
        )

    # ── Step 3: Fetch full details only for NEW device IDs ────────────────────
    connector_map = {c.platform_name: c for c in connectors}
    all_new_devices: list[DeviceRecord] = []

    async def _fetch_new(platform: str, new_ids: list[str]):
        connector = connector_map.get(platform)
        if not connector:
            return []
        if new_ids == ["__FULL_FETCH__"]:
            # Probe failed — full fetch for this platform
            try:
                return await connector.get_devices()
            except Exception as exc:
                log.warning("smart_reconcile.full_fetch_failed", platform=platform, error=str(exc))
                return []
        if not new_ids:
            return []
        # If we already fetched all devices during the probe step (no custom
        # get_device_ids), filter from the cached list instead of re-fetching.
        if platform in _prefetched_devices:
            id_set = set(new_ids)
            return [d for d in _prefetched_devices[platform] if d.platform_id in id_set]
        try:
            return await connector.get_devices_for_ids(new_ids)
        except Exception as exc:
            log.warning("smart_reconcile.partial_fetch_failed", platform=platform, error=str(exc))
            return []

    fetch_tasks = [_fetch_new(plat, ids) for plat, ids in platform_new_ids.items()]
    fetch_results_lists = await asyncio.gather(*fetch_tasks)
    for devices in fetch_results_lists:
        all_new_devices.extend(devices)

    log.info("smart_reconcile.new_devices_fetched", count=len(all_new_devices))

    # ── Step 4: Remove stale IDs + upsert new devices ─────────────────────────
    async with factory() as session:
        # Delete removed IDs
        for platform, removed_ids in platform_removed_ids.items():
            if removed_ids:
                from sqlalchemy import delete as sa_delete
                await session.execute(
                    sa_delete(PlatformDeviceRow).where(
                        (PlatformDeviceRow.platform == platform) &
                        (PlatformDeviceRow.platform_id.in_(removed_ids))
                    )
                )
                log.info("smart_reconcile.removed", platform=platform, count=len(removed_ids))

        # Upsert new devices
        if all_new_devices:
            alias_repo = CompanyAliasRepository(session)
            alias_map = await alias_repo.get_alias_map()

            # Build alias map using ALL available device data so cross-platform
            # company names resolve correctly.  Prefetched devices (unchanged
            # platforms) are used for alias building ONLY — not re-upserted.
            new_groups: dict[str, list[DeviceRecord]] = {}
            all_groups_for_aliases: dict[str, list[DeviceRecord]] = {}
            for d in all_new_devices:
                new_groups.setdefault(d.platform, []).append(d)
                all_groups_for_aliases.setdefault(d.platform, []).append(d)
            for plat, devs in _prefetched_devices.items():
                if devs:
                    all_groups_for_aliases.setdefault(plat, []).extend(devs)

            # CPU-bound matching runs in a worker thread so the app keeps answering requests
            device_aliases = await asyncio.to_thread(
                build_device_cross_reference_aliases, list(all_groups_for_aliases.items()), alias_map
            )
            companies_by_platform: dict[str, list] = {plat: [] for plat in all_groups_for_aliases}
            auto_aliases = await asyncio.to_thread(build_cross_platform_aliases, companies_by_platform, alias_map)
            merged_alias_map = {**auto_aliases, **device_aliases, **alias_map}

            # Only upsert the genuinely new/changed device rows
            device_rows = await asyncio.to_thread(
                prepare_device_rows, list(new_groups.items()), alias_map=merged_alias_map
            )
            plat_repo = PlatformDeviceRepository(session)
            await plat_repo.bulk_upsert(device_rows)

            log.info(
                "smart_reconcile.upserted",
                new_devices=len(all_new_devices),
                rows=len(device_rows),
                platforms=sorted(new_groups.keys()),
            )
        else:
            log.info("smart_reconcile.no_new_devices", note="All platforms unchanged")

        await session.commit()

    # ── Step 5: Refresh AV assignment cache + re-run reconcile_from_db ──
    # run_fast_reconciliation saves its own SyncRun record — no need to save
    # a second one here.  We use its run_id as the canonical ID for this run.
    await _refresh_av_assignment_cache(connectors)
    fast_result = await run_fast_reconciliation()

    total_removed = sum(len(v) for v in platform_removed_ids.values())

    result = {
        "run_id": fast_result.get("run_id", run_id),
        "mode": "smart",
        "new_devices_fetched": len(all_new_devices),
        "removed_from_db": total_removed,
        "platforms_probed": len(connectors),
        "companies": fast_result.get("companies", 0),
        "matched": fast_result.get("matched", 0),
        "total_unique": fast_result.get("total_unique", 0),
        "note": f"Fetched details for {len(all_new_devices)} new devices only. "
                f"Reused DB data for unchanged devices.",
    }
    log.info("smart_reconciliation.complete", **{k: v for k, v in result.items() if k != "note"})
    return result


def _compute_ep(dev: dict, assigned_av: str, *, av_evidence_available: bool = True) -> str:
    """Compute endpoint_protection label. Mirrors companies.py logic.

    When AV evidence is unavailable (e.g. AV inventory table is empty), avoid
    classifying ESET-assigned devices as definitively missing ESET.
    """
    eset_ver = dev.get("eset_version", "")
    s1_ver = dev.get("s1_agent_version", "")
    dev_has_s1 = "sentinelone" in dev.get("platforms", [])
    dev_has_eset = dev.get("eset_installed") or "eset" in dev.get("av_vendors", [])
    if assigned_av == "sentinelone":
        if dev_has_s1 and s1_ver:
            return f"SentinelOne {s1_ver}".strip()
        if dev_has_s1:
            return "SentinelOne"
        return "None (SentinelOne expected)"
    if assigned_av == "eset":
        if dev_has_eset and eset_ver:
            return f"ESET {eset_ver}".strip()
        if dev_has_eset:
            return "ESET"
        if dev_has_s1:
            return "SentinelOne (ESET expected)"
        if not av_evidence_available:
            return "Unknown (ESET evidence unavailable)"
        return "None (ESET expected)"
    if dev_has_s1 and s1_ver:
        return f"SentinelOne {s1_ver}"
    if dev_has_eset and eset_ver:
        return f"ESET {eset_ver}"
    if dev_has_s1:
        return "SentinelOne"
    if dev_has_eset:
        return "ESET"
    return "None"


async def run_fast_reconciliation(*, record_run: bool = True) -> dict[str, Any]:
    """Re-run matching + snapshot on existing platform_devices — no live API calls.

    Flow:
    1. Load platform_devices from DB (already populated by last full reconcile)
    2. Compare per-(company, platform) counts against yesterday's snapshot
    3. Re-run reconcile_from_db to rebuild reconciled_devices
    4. Take a new snapshot row for today
    5. Return a summary of what changed vs the last snapshot

    Typical runtime: 2-5 seconds vs 60-90s for a full reconcile.
    Use this for hourly/on-demand checks. Schedule full reconcile once daily.
    """
    from datetime import date
    from app.db.schema import PlatformDevice as PlatformDeviceRow

    run_id = str(uuid4())
    log.info("fast_reconciliation.start", run_id=run_id)

    # Self-heal schema drift on live systems before ORM queries run.
    await _ensure_platform_devices_schema()

    factory = get_session_factory()

    # Group devices by registry client (ID links) instead of fuzzy names — own session, before rows are loaded
    registry: dict[str, dict[str, Any]] = {}
    try:
        from app.services.client_registry import apply_registry_to_devices
        async with factory() as reg_session:
            registry = await apply_registry_to_devices(reg_session)
    except Exception as exc:  # noqa: BLE001 — never block the breakdown on the registry
        log.warning("fast_reconciliation.registry_keying_failed", error=str(exc))

    async with factory() as session:
        from sqlalchemy import func, select as sa_select
        # Quick count check vs yesterday's snapshot
        today     = date.today().isoformat()
        yesterday = (date.today() - __import__('datetime').timedelta(days=1)).isoformat()

        # Current platform_devices counts
        current_counts = (await session.execute(
            sa_select(
                PlatformDeviceRow.company_canonical,
                PlatformDeviceRow.platform,
                func.count().label("cnt"),
            )
            .where(PlatformDeviceRow.company_canonical != "")
            .group_by(PlatformDeviceRow.company_canonical, PlatformDeviceRow.platform)
        )).all()

        total_devices = sum(r.cnt for r in current_counts)

        if total_devices == 0:
            return {
                "run_id": run_id,
                "mode": "fast",
                "error": "platform_devices is empty — run a full reconcile first",
                "total_devices": 0,
            }

        # ── Reconcile device-level matching from platform_devices ──
        from app.services.device_identity import reconcile_from_db
        from app.db.schema import ReconciledDevice as ReconciledDeviceRow
        from app.db.repository import (
            AvAssignmentCacheRepository,
            BreakdownCacheRepository,
            BreakdownSnapshotRepository,
            DeviceAvStatusRepository,
            ReconciledDeviceRepository,
        )

        orm_devices = (await session.execute(sa_select(PlatformDeviceRow))).scalars().all()
        av_repo = DeviceAvStatusRepository(session)
        av_lookup = await av_repo.get_av_lookup()
        av_evidence_available = bool(av_lookup)
        breakdown = await asyncio.to_thread(reconcile_from_db, orm_devices, av_lookup=av_lookup)

        # ── Enrich with AV assignments from DB cache (no live API calls) ──
        av_cache_repo = AvAssignmentCacheRepository(session)
        av_map = await av_cache_repo.get_map()

        def _has_psa(company_data: dict) -> bool:
            for dev in company_data.get("matched", []):
                if "connectwise_psa" in dev.get("platforms", []):
                    return True
            if "connectwise_psa" in company_data.get("mismatched", {}):
                return True
            return company_data.get("company", "") in av_map

        for company_data in breakdown:
            company_name = company_data["company"]
            company_data["psa_present"] = _has_psa(company_data)
            client = registry.get(company_name)
            if client:
                company_data.update(client_id=client["client_id"], client_code=client["client_code"],
                                    standard_name=client["standard_name"])
            # Expected AV: the registry client's AV (linked companies), else the PSA agreement map
            client_av = (client or {}).get("av_vendor")
            company_data["assigned_av"] = client_av if client_av in ("sentinelone", "eset") \
                else av_map.get(company_name, "none")
            assigned = company_data["assigned_av"]

            has_s1 = (
                any("sentinelone" in d.get("platforms", []) for d in company_data.get("matched", []))
                or "sentinelone" in company_data.get("mismatched", {})
            )
            company_data["s1_present"] = has_s1
            # eset_present is set by reconcile_from_db via av_lookup;
            # fall back to portal check if not set
            if "eset_present" not in company_data:
                company_data["eset_present"] = any(
                    "eset" in dev.get("av_vendors", [])
                    for dev in company_data.get("matched", [])
                )

            for dev in company_data.get("matched", []):
                dev["endpoint_protection"] = _compute_ep(
                    dev,
                    assigned,
                    av_evidence_available=av_evidence_available,
                )
            for _plat, devs in company_data.get("mismatched", {}).items():
                for dev in devs:
                    dev["endpoint_protection"] = _compute_ep(
                        dev,
                        assigned,
                        av_evidence_available=av_evidence_available,
                    )

            # Annotate counts for the cache
            company_data["matched_count"] = len(company_data.get("matched", []))
            company_data["mismatched_count"] = sum(
                len(v) for v in company_data.get("mismatched", {}).values()
            )

        # ── Write to breakdown_cache (replaces old reconciled_devices path) ──
        bk_repo = BreakdownCacheRepository(session)
        await bk_repo.replace_all(breakdown, run_id=run_id)

        # ── Write to reconciled_devices for backwards-compat DB tools ──
        rows: list[ReconciledDeviceRow] = []
        for company_data in breakdown:
            company = company_data.get("company", "")
            if not company:
                continue
            for dev in company_data.get("matched", []):
                rows.append(ReconciledDeviceRow(
                    id=str(uuid4()),
                    company_name=company,
                    site_name=dev.get("site_name", "") or "",
                    hostname=dev.get("hostname", "") or "",
                    endpoint_id=dev.get("endpoint_id", "") or "",
                    mac_address=",".join(dev.get("mac_addresses") or []),
                    serial_number=dev.get("serial_number", "") or "",
                    platforms=dev.get("platforms", []),
                    match_type="full" if len(dev.get("platforms", [])) >= 3 else "partial",
                ))
            for platform, devs in (company_data.get("mismatched") or {}).items():
                for dev in devs:
                    rows.append(ReconciledDeviceRow(
                        id=str(uuid4()),
                        company_name=company,
                        site_name=dev.get("site_name", "") or "",
                        hostname=dev.get("hostname", "") or "",
                        endpoint_id=dev.get("endpoint_id", "") or "",
                        mac_address=",".join(dev.get("mac_addresses") or []),
                        serial_number=dev.get("serial_number", "") or "",
                        platforms=[platform],
                        match_type="unmatched",
                    ))

        rec_repo = ReconciledDeviceRepository(session)
        await rec_repo.replace_all(rows)

        # ── Breakdown snapshot + change diff ──
        snap_repo = BreakdownSnapshotRepository(session)
        previous_snap = await snap_repo.latest()
        await snap_repo.save(run_id=run_id, companies=breakdown, previous=previous_snap)

        await session.commit()

    # Take today's daily platform-level snapshot
    await _take_daily_snapshot(run_id)

    matched   = sum(1 for r in rows if r.match_type != "unmatched")
    unmatched = sum(1 for r in rows if r.match_type == "unmatched")
    total_unique = len(rows)

    if record_run:
        await _save_sync_run(
            run_id=run_id,
            status="completed",
            company_match_count=len(breakdown),
            device_match_count=total_unique,
        )

    log.info(
        "fast_reconciliation.complete",
        run_id=run_id,
        total_devices=total_devices,
        companies=len(breakdown),
        matched=matched,
        unmatched=unmatched,
    )

    # Bust the in-memory cache so the next GET re-reads from breakdown_cache.
    try:
        from app.api.companies import invalidate_reconciled_cache
        invalidate_reconciled_cache()
    except Exception:  # noqa: BLE001
        pass

    return {
        "run_id": run_id,
        "mode": "fast",
        "total_devices": total_devices,
        "matched": matched,
        "unmatched": unmatched,
        "total_unique": total_unique,
        "companies": len(breakdown),
        "note": "No live API calls — used existing platform_devices data. Run mode=full to refresh from APIs.",
    }


async def _remove_vanished_devices(session: Any, fetch_results: list[Any]) -> dict[str, int]:
    """Delete stored devices a platform no longer reports.

    Full mode used to only upsert, so machines deleted in a tool (e.g. SentinelOne) stayed in the breakdown
    forever. Only platforms that fetched cleanly AND returned devices are pruned — an error or an empty response
    never wipes stored data.
    """
    from sqlalchemy import delete as sa_delete, select as sa_select

    from app.db.schema import PlatformDevice as _PD

    removed: dict[str, int] = {}
    for result in fetch_results:
        if result.error or not result.devices:
            continue
        live_ids = {str(d.platform_id) for d in result.devices if d.platform_id}
        stale = [r[0] for r in (await session.execute(
            sa_select(_PD.id, _PD.platform_id).where(_PD.platform == result.platform))).all()
            if str(r[1]) not in live_ids]
        for i in range(0, len(stale), 500):
            await session.execute(sa_delete(_PD).where(_PD.id.in_(stale[i:i + 500])))
        if stale:
            removed[result.platform] = len(stale)
            log.info("reconciliation.platform_devices_removed", platform=result.platform,
                     removed=len(stale), still_reported=len(live_ids))
    if removed:
        await session.commit()
    return removed


def _pairwise_matches(
    all_companies: list[CompanyRecord], all_devices: list[DeviceRecord],
) -> tuple[list[CompanyMatchResult], list[DeviceMatchResult]]:
    """Every record against every candidate on each other platform (pure CPU — call via asyncio.to_thread)."""
    companies: list[CompanyMatchResult] = []
    devices: list[DeviceMatchResult] = []
    by_plat_c: dict[str, list[CompanyRecord]] = {}
    for company in all_companies:
        by_plat_c.setdefault(company.platform, []).append(company)
    for plat_a, plat_b in itertools.combinations(list(by_plat_c), 2):
        for company in by_plat_c[plat_a]:
            companies.append(match_company(company, by_plat_c[plat_b]))
    by_plat_d: dict[str, list[DeviceRecord]] = {}
    for device in all_devices:
        by_plat_d.setdefault(device.platform, []).append(device)
    for plat_a, plat_b in itertools.combinations(list(by_plat_d), 2):
        for device in by_plat_d[plat_a]:
            devices.append(match_device(device, by_plat_d[plat_b]))
    return companies, devices


async def run_full_reconciliation(
    connectors: list[AbstractPlatformConnector],
) -> ReconciliationReport:
    """Fetch data from all connectors concurrently and run cross-platform matching.

    Persists a SyncRun with per-connector events for full visibility.
    """
    run_id = str(uuid4())
    log.info("reconciliation.start", run_id=run_id, num_connectors=len(connectors))

    # Self-heal schema drift on live systems before ORM queries run.
    await _ensure_platform_devices_schema()

    # Persist run start
    factory = get_session_factory()
    async with factory() as session:
        repo = SyncRunRepository(session)
        await repo.create_run(run_id)

    fetch_results: list[ConnectorFetchResult] = []
    all_companies: list[CompanyRecord] = []
    all_devices: list[DeviceRecord] = []
    company_matches: list[CompanyMatchResult] = []
    device_matches: list[DeviceMatchResult] = []
    warnings: list[str] = []
    run_finished = False

    try:
        # Fetch all connectors concurrently
        fetch_results = await asyncio.gather(*[_fetch_platform(c) for c in connectors])

        # Persist per-connector events AND bulk-upsert raw devices into platform_devices
        async with factory() as session:
            repo = SyncRunRepository(session)
            device_repo = PlatformDeviceRepository(session)
            for result in fetch_results:
                event_type = "success" if result.error is None else "error"
                await repo.add_connector_event(
                    run_id=run_id,
                    connector=result.platform,
                    event_type=event_type,
                    message=result.error or "",
                    company_count=len(result.companies),
                    device_count=len(result.devices),
                    latency_ms=result.latency_ms,
                )
                all_companies.extend(result.companies)
                all_devices.extend(result.devices)
                if result.error:
                    warnings.append(result.error)

            # Bulk-upsert all fetched devices into platform_devices so db_* tools work
            if all_devices:
                from app.services.device_identity import prepare_device_rows
                from app.services.company_matcher import (
                    build_cross_platform_aliases,
                    build_device_cross_reference_aliases,
                )
                from app.db.repository import CompanyAliasRepository, CompanyMappingRepository

                alias_repo = CompanyAliasRepository(session)
                alias_map = await alias_repo.get_alias_map()

                # Build cross-platform auto-aliases (handles ASIO truncation vs PSA full names)
                companies_by_platform: dict[str, list] = {}
                for result in fetch_results:
                    companies_by_platform.setdefault(result.platform, []).extend(result.companies)

                auto_aliases = await asyncio.to_thread(
                    build_cross_platform_aliases, companies_by_platform, alias_map
                )
                device_aliases = await asyncio.to_thread(
                    build_device_cross_reference_aliases,
                    [(r.platform, r.devices) for r in fetch_results], alias_map,
                )
                merged_alias_map = {**auto_aliases, **device_aliases, **alias_map}

                # Save the merged alias map to company_mappings so reconcile_from_db uses it
                from app.services.device_identity import prepare_company_mappings
                from app.models.company import CompanyRecord
                all_companies_flat: list[CompanyRecord] = []
                for result in fetch_results:
                    all_companies_flat.extend(result.companies)

                mapping_rows = prepare_company_mappings(
                    [(r.platform, r.devices) for r in fetch_results],
                    alias_map=merged_alias_map,
                )
                mapping_repo = CompanyMappingRepository(session)
                await mapping_repo.bulk_upsert(mapping_rows)

                # Group devices by platform for prepare_device_rows
                platform_groups: dict[str, list[DeviceRecord]] = {}
                for d in all_devices:
                    platform_groups.setdefault(d.platform, []).append(d)

                device_rows = await asyncio.to_thread(
                    prepare_device_rows,
                    list(platform_groups.items()),
                    alias_map=merged_alias_map,   # use full merged map
                )
                upsert_stats = await device_repo.bulk_upsert(device_rows)
                log.info(
                    "reconciliation.platform_devices_upserted",
                    inserted=upsert_stats.get("inserted", 0),
                    updated=upsert_stats.get("updated", 0),
                    total=len(device_rows),
                )

                # Remove devices a platform no longer reports (deleted/retired machines)
                await _remove_vanished_devices(session, fetch_results)

        # Legacy pairwise company/device matching — the heaviest part (N×M per platform pair), so it runs
        # in a worker thread; before this it froze every other request for the whole run.
        pair_companies, pair_devices = await asyncio.to_thread(_pairwise_matches, all_companies, all_devices)
        company_matches.extend(pair_companies)
        device_matches.extend(pair_devices)

        # Persist match results to the database (clear old results first)
        async with factory() as session:
            repo = ReconciliationRepository(session)
            await repo.clear_company_matches()
            await repo.save_company_matches(company_matches, run_id=run_id)
            await repo.clear_device_matches()
            await repo.save_device_matches(device_matches, run_id=run_id)

        log.info(
            "reconciliation.matches_saved",
            run_id=run_id,
            company_matches=len(company_matches),
            device_matches=len(device_matches),
        )

        # ── Refresh AV assignment cache from PSA so run_fast_reconciliation
        # can enrich the breakdown without live API calls ──────────────────
        await _refresh_av_assignment_cache(connectors)

        # Rebuild everything the UI and tools read (breakdown_companies, reconciled_devices, snapshots) from the
        # freshly-upserted platform_devices. Full mode used to skip breakdown_companies, leaving the Breakdown
        # tab stale after a full run. record_run=False: this run's own row gets the numbers (one row per run).
        fast = await run_fast_reconciliation(record_run=False)

        # Finish the run — connector failures are warnings, not run-level errors.
        # The run is "completed" if reconciliation logic succeeded, regardless
        # of individual connector availability (e.g. ESET API returning 500).
        # Recorded counts = breakdown companies + unique machines, the same numbers smart/fast runs record
        # (not the legacy pairwise comparison counts, which aren't company or machine totals).
        run_status = "completed"
        async with factory() as session:
            repo = SyncRunRepository(session)
            await repo.finish_run(
                run_id,
                status=run_status,
                company_match_count=int(fast.get("companies", 0) or 0),
                device_match_count=int(fast.get("total_unique", 0) or 0),
                error_count=0,
                errors=None,
                warning_count=len(warnings),
                warnings=warnings if warnings else None,
            )
        run_finished = True

        log.info(
            "reconciliation.complete",
            run_id=run_id,
            status=run_status,
            companies=fast.get("companies"),
            machines=fast.get("total_unique"),
            pairwise_company_matches=len(company_matches),
            pairwise_device_matches=len(device_matches),
            warnings=len(warnings),
        )

        return ReconciliationReport(
            run_id=run_id,
            company_matches=company_matches,
            device_matches=device_matches,
            errors=warnings,
            connector_results=fetch_results,
        )
    except Exception as exc:  # noqa: BLE001
        error_message = f"{type(exc).__name__}: {exc}"
        log.error(
            "reconciliation.failed",
            run_id=run_id,
            error=error_message,
            company_matches=len(company_matches),
            device_matches=len(device_matches),
            warnings=len(warnings),
            exc_info=True,
        )

        if not run_finished:
            try:
                async with factory() as session:
                    repo = SyncRunRepository(session)
                    await repo.finish_run(
                        run_id,
                        status="failed",
                        company_match_count=len(company_matches),
                        device_match_count=len(device_matches),
                        error_count=1,
                        errors=[error_message],
                        warning_count=len(warnings),
                        warnings=warnings if warnings else None,
                    )
            except Exception as finish_exc:  # noqa: BLE001
                log.error(
                    "reconciliation.failed_to_mark_failed",
                    run_id=run_id,
                    error=str(finish_exc),
                    exc_info=True,
                )

        raise
