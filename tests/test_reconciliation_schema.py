"""The platform_devices self-heal that runs before every reconciliation works on SQLite too."""

import asyncio

from sqlalchemy import inspect, text

from app.services.reconciliation import _ensure_platform_devices_schema


def _columns(factory) -> set[str]:  # type: ignore[no-untyped-def]
    async def _get() -> set[str]:
        async with factory() as db:
            conn = await db.connection()
            return await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("platform_devices")}
            )

    return asyncio.run(_get())


def test_schema_check_is_idempotent_and_adds_missing_columns(db_factory):  # type: ignore[no-untyped-def]
    asyncio.run(_ensure_platform_devices_schema())  # columns already there: no-op, no error
    asyncio.run(_ensure_platform_devices_schema())

    async def _drop() -> None:  # an older database without client_id
        async with db_factory() as db:
            await db.execute(text("DROP INDEX IF EXISTS ix_platform_devices_client_id"))
            await db.execute(text("ALTER TABLE platform_devices DROP COLUMN client_id"))
            await db.commit()

    asyncio.run(_drop())
    assert "client_id" not in _columns(db_factory)
    asyncio.run(_ensure_platform_devices_schema())
    assert {"client_id", "company_platform_id"} <= _columns(db_factory)


def test_daily_snapshot_upsert_on_sqlite(db_factory):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from app.db.schema import DailySnapshot, ReconciledDevice
    from app.services.reconciliation import _take_daily_snapshot

    async def _seed() -> None:
        async with db_factory() as db:
            db.add_all([
                ReconciledDevice(id="d1", company_name="Acme", hostname="PC1", platforms={},
                                 match_type="exact"),
                ReconciledDevice(id="d2", company_name="Acme", hostname="PC2", platforms={},
                                 match_type="unmatched"),
            ])
            await db.commit()

    async def _rows() -> list:  # type: ignore[type-arg]
        async with db_factory() as db:
            return list((await db.execute(select(DailySnapshot))).scalars())

    asyncio.run(_seed())
    asyncio.run(_take_daily_snapshot("run-1"))
    asyncio.run(_take_daily_snapshot("run-2"))  # same day: updates the row instead of failing
    rows = asyncio.run(_rows())
    assert len(rows) == 1
    assert (rows[0].total_devices, rows[0].matched_count, rows[0].run_id) == (2, 1, "run-2")
