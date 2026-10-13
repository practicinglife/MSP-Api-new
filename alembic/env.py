"""Alembic environment.

Used two ways: by the app at startup (``app/db/migrations.py`` hands over its open connection in
``config.attributes["connection"]``), and from the command line (``alembic upgrade head``), which connects to
``DATABASE_URL``.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.config import get_settings
from app.db.schema import Base

config = context.config
target_metadata = Base.metadata


def do_run_migrations(connection):  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                      render_as_batch=connection.dialect.name == "sqlite")  # SQLite needs batch ALTERs
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(get_settings().database_url)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
        await connection.commit()
    await connectable.dispose()


_shared = config.attributes.get("connection")
if _shared is not None:
    do_run_migrations(_shared)
else:
    if config.config_file_name is not None:
        fileConfig(config.config_file_name)
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        asyncio.run(run_migrations_online())
