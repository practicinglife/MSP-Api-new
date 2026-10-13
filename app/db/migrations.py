"""Database schema migrations (Alembic), run automatically at startup.

* **New database** → every migration from the baseline (``0001_baseline``) up.
* **Database from before migrations existed** (tables made by ``create_all`` + ad-hoc repairs): the old repairs
  run once. They build every missing table and column from the current models, so the database is then at
  the latest shape and is stamped as head (stamping the baseline would make later migrations re-create what
  the repair just built). No data is dropped.
* **Up-to-date database** → nothing to do.

Changing a model means adding a migration: ``alembic revision --autogenerate -m "what changed"`` (run with
``DATABASE_URL`` pointing at an up-to-date database), then review the file. ``tests/test_migrations.py`` fails
when the models and the migrations disagree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from app.db.schema import Base

log = structlog.get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
BASELINE = "0001_baseline"


def config(connection: Any = None) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def head() -> str | None:
    return ScriptDirectory.from_config(config()).get_current_head()


def _current(sync_conn: Any) -> str | None:
    return MigrationContext.configure(sync_conn).get_current_revision()


def _upgrade_sync(sync_conn: Any) -> dict[str, Any]:
    tables = set(inspect(sync_conn).get_table_names())
    known = {r.revision for r in ScriptDirectory.from_config(config()).walk_revisions()}
    current = _current(sync_conn) if "alembic_version" in tables else None
    adopted = False
    if current not in known and tables & set(Base.metadata.tables):
        # Made before migrations (or by an old, unrelated revision): repair to the current models, record head.
        legacy_repair(sync_conn)
        command.stamp(config(sync_conn), "head", purge=True)
        adopted = True
        log.info("db.migrate.adopted_existing_database", previous=current)
    command.upgrade(config(sync_conn), "head")
    return {"adopted": adopted, "from": current, "to": _current(sync_conn)}


async def upgrade(engine: AsyncEngine) -> dict[str, Any]:
    async with engine.begin() as conn:
        result = await conn.run_sync(_upgrade_sync)
    log.info("db.migrate.done", **result)
    return result


async def status() -> dict[str, Any]:
    from app.db.repository import _make_engine

    engine = _make_engine()
    try:
        async with engine.connect() as conn:
            current = await conn.run_sync(_current)
    except Exception as exc:  # noqa: BLE001
        return {"current": None, "head": head(), "up_to_date": False, "error": str(exc)}
    finally:
        await engine.dispose()
    latest = head()
    return {"current": current, "head": latest, "up_to_date": current == latest}


# ── Pre-migration databases ───────────────────────────────────────────────


def legacy_repair(sync_conn: Any) -> None:
    """The repairs ``init_db`` used to run on every start: create missing tables and add columns that older
    tables lack, so the database matches the baseline before it is stamped."""
    Base.metadata.create_all(sync_conn)
    insp = inspect(sync_conn)
    for table_name in ("company_matches", "device_matches"):
        if insp.has_table(table_name) and "run_id" not in {c["name"] for c in insp.get_columns(table_name)}:
            sync_conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN run_id VARCHAR(36)"))
            log.info("db.migrate.add_column", table=table_name, column="run_id")

    # Columns added after these tables first shipped, with the defaults existing rows need
    later_columns = {
        "daily_snapshots": [("total_devices", "INTEGER NOT NULL DEFAULT 0"),
                            ("matched_count", "INTEGER NOT NULL DEFAULT 0"),
                            ("unmatched_count", "INTEGER NOT NULL DEFAULT 0"),
                            ("asio_count", "INTEGER NOT NULL DEFAULT 0"), ("psa_count", "INTEGER NOT NULL DEFAULT 0"),
                            ("sc_count", "INTEGER NOT NULL DEFAULT 0"), ("s1_count", "INTEGER NOT NULL DEFAULT 0"),
                            ("run_id", "VARCHAR(36)"), ("taken_at", "TIMESTAMPTZ DEFAULT NOW()")],
        "platform_devices": [("company_platform_id", "VARCHAR(256) NOT NULL DEFAULT ''"), ("client_id", "VARCHAR(36)")],
        "asio_endpoint_av": [("last_contact", "VARCHAR(40) NOT NULL DEFAULT ''"), ("online", "BOOLEAN")],
        "oauth_tokens": [("user_upn", "VARCHAR(255)"), ("user_roles", "JSON")],
    }
    for table_name, cols in later_columns.items():
        if not insp.has_table(table_name):
            continue
        existing = {c["name"] for c in insp.get_columns(table_name)}
        for col_name, col_def in cols:
            if col_name not in existing:
                if sync_conn.dialect.name != "postgresql":
                    col_def = col_def.replace("TIMESTAMPTZ DEFAULT NOW()", "TIMESTAMP")
                sync_conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}"))
                log.info("db.migrate.add_column", table=table_name, column=col_name)

    # api_key_id holds "m365:<oid>" (41 chars) for Microsoft 365 sign-ins
    if insp.has_table("oauth_tokens") and sync_conn.dialect.name == "postgresql":
        ak = next((c for c in insp.get_columns("oauth_tokens") if c["name"] == "api_key_id"), None)
        if ak is not None and getattr(ak["type"], "length", None) and ak["type"].length < 64:
            sync_conn.execute(text("ALTER TABLE oauth_tokens ALTER COLUMN api_key_id TYPE VARCHAR(64)"))
            log.info("db.migrate.widen_column", table="oauth_tokens", column="api_key_id")

    # Any column the models define but an older table lacks (added as NULLable so existing rows stay valid)
    insp = inspect(sync_conn)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing or col.primary_key:
                continue
            ddl = col.type.compile(dialect=sync_conn.dialect)
            sync_conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}'))
            log.info("db.migrate.add_missing_column", table=table.name, column=col.name, type=ddl)
