"""Schema migrations: fresh databases, databases from before migrations, and models ↔ migrations agreement.

All on temporary SQLite files (the same migrations run on PostgreSQL in production).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine

from app.db.migrations import BASELINE, head
from app.db.repository import init_db
from app.db.schema import Base


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _revision(path: Path) -> str | None:
    con = sqlite3.connect(path)
    try:
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        con.close()
    return row[0] if row else None


def _diff(path: Path) -> list[object]:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    engine.dispose()
    return diffs


def test_fresh_database_gets_every_table(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "fresh.db"
    asyncio.run(init_db(_url(db)))
    assert _revision(db) == head()
    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert set(Base.metadata.tables) <= tables


def test_models_and_migrations_agree(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Fails when a model changed without a migration: add one with
    ``alembic revision --autogenerate -m "…"`` and review it."""
    db = tmp_path / "agree.db"
    asyncio.run(init_db(_url(db)))
    assert _diff(db) == []


def test_database_from_before_migrations_is_adopted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    tables = [t for name, t in Base.metadata.tables.items() if name not in ("connection_syncs", "connection_records")]
    Base.metadata.create_all(engine, tables=tables)  # an older install: two tables missing …
    engine.dispose()
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE local_users DROP COLUMN last_login_at")  # … and a column added later
    con.execute("INSERT INTO local_users (username, password_hash, totp_secret, disabled, failed_count, "
                "last_totp_step, created_at) VALUES ('keepme', 'x', 'y', 0, 0, 0, '2026-01-01 00:00:00')")
    con.commit()
    con.close()

    asyncio.run(init_db(_url(db)))
    assert _revision(db) == head()
    con = sqlite3.connect(db)
    assert con.execute("SELECT username FROM local_users").fetchall() == [("keepme",)]  # data kept
    cols = {r[1] for r in con.execute("PRAGMA table_info(local_users)")}
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "last_login_at" in cols and {"connection_syncs", "connection_records"} <= tables


def test_unknown_old_revision_is_adopted_and_rerun_is_a_no_op(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "old-revision.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    Base.metadata.create_all(engine)
    engine.dispose()
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
    con.execute("INSERT INTO alembic_version VALUES ('0001')")  # the old, unrelated first migration
    con.commit()
    con.close()

    asyncio.run(init_db(_url(db)))
    assert _revision(db) == head()
    asyncio.run(init_db(_url(db)))  # second start: nothing to do
    assert _revision(db) == head()
    assert BASELINE == "0001_baseline"


def test_hand_made_read_only_labels_on_writes_are_reset(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """0004: a POST labelled read-only by hand goes back through the write mode; a template's POST reads stay."""
    from alembic import command
    from app.db.migrations import config

    db = tmp_path / "labels.db"
    asyncio.run(init_db(_url(db)))
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    with engine.begin() as conn:
        command.downgrade(config(conn), "0003_three_roles")
    con = sqlite3.connect(db)
    con.execute("INSERT INTO connections (id, slug, name, kind, base_url, auth_type, description, enabled, "
                "operation_count, settings, created_by, created_at, updated_at) VALUES "
                "('c1', 'sc', 'SC', 'openapi', '', 'none', '', 1, 0, '{\"template\": \"screenconnect\"}', 'x', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00'), ('c2', 'pets', 'Pets', 'openapi', '', 'none', '', 1, 0, '{}', 'x', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')")
    rows = [("o1", "c1", "POST", "/GetSessionsByFilter", 1), ("o2", "c1", "POST", "/SendCommandToSession", 1),
            ("o3", "c2", "POST", "/pets", 1), ("o4", "c2", "GET", "/pets", 1), ("o5", "c2", "DELETE", "/pets/1", 0)]
    con.executemany("INSERT INTO operations (id, connection_id, key, name, title, description, method, target, "
                    "read_only, pinned, deprecated, enabled, search_text) VALUES "
                    "(?, ?, ?, ?, '', '', ?, ?, ?, 0, 0, 1, '')",
                    [(i, c, i, i, m, t, ro) for i, c, m, t, ro in rows])
    con.commit()
    con.close()
    with engine.begin() as conn:
        command.upgrade(config(conn), "head")
    engine.dispose()

    con = sqlite3.connect(db)
    got = {r[0]: r[1:] for r in con.execute(
        "SELECT id, read_only, defined_read_only, read_only_template FROM operations")}
    con.close()
    assert got == {"o1": (1, 0, 1), "o2": (0, 0, 0), "o3": (0, 0, 0), "o4": (1, 1, 0), "o5": (0, 0, 0)}
