"""Shared test fixtures and configuration."""

import asyncio
import os
import secrets
import unittest.mock as mock
from datetime import UTC, datetime, timedelta

# ── Isolation: tests must never see real credentials or reach a real system ──
# 1. No .env: neither our loader nor pydantic-settings' own env_file reading (patched below).
os.environ["MSP_SKIP_DOTENV"] = "1"
os.environ["MSP_MASTER_KEY"] = "dGVzdC1tYXN0ZXIta2V5LTMyLWJ5dGVzLWxvbmctISE="  # 32 bytes, tests only
# 2. No background jobs, and no DNS lookups by the outbound-request guard (test hosts are fictional).
os.environ["MSP_NIGHTLY"] = "off"
os.environ["MSP_S1_TRIAGE_MINUTES"] = "0"
os.environ["MSP_CONNECTION_SYNC"] = "off"
# 3. The app under test has the MSP pack on (its tests need it); tests/test_packs.py covers core-only installs.
os.environ["MSP_PACKS"] = "msp"
os.environ["MSP_NETGUARD_RESOLVE"] = "0"
for _var in ("MSP_ENTRA_TENANT_ID", "MSP_ENTRA_CLIENT_ID", "MSP_ENTRA_CLIENT_SECRET", "MSP_OIDC_ISSUER",
             "MSP_WRITE_MODE_DEFAULT"):
    os.environ.pop(_var, None)

import socket  # noqa: E402

import app.config as _app_config  # noqa: E402

_app_config.Settings.model_config["env_file"] = None
_app_config.get_settings.cache_clear()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.db.schema import Base  # noqa: E402

ADMIN_ROLES = ["MSPTools.User", "MSPTools.Editor", "MSPTools.Approver", "MSPTools.Admin"]

_real_connect = socket.socket.connect


def _guarded_connect(self, address):  # type: ignore[no-untyped-def]
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and not host.startswith("/"):
        raise RuntimeError(f"Tests may not open network connections (tried {address!r}) — mock it with respx")
    return _real_connect(self, address)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):  # type: ignore[no-untyped-def]
    """3. Any real outbound connection fails the test (respx mocks never reach a socket)."""
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    yield


@pytest.fixture
async def async_session():  # type: ignore[return]
    """Provide an in-memory SQLite async session for tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
def db_factory(tmp_path):  # type: ignore[no-untyped-def]
    """A fresh SQLite database (file-backed, so every event loop sees the same data) wired into the app."""
    import app.db.repository as repo_module
    from app.auth import ratelimit

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_setup())
    original = repo_module._default_factory
    repo_module._default_factory = factory  # type: ignore[attr-defined]
    ratelimit.reset()
    yield factory
    repo_module._default_factory = original  # type: ignore[attr-defined]
    asyncio.run(engine.dispose())


def make_session(factory, roles: list[str], upn: str = "tester@example.com") -> str:  # type: ignore[no-untyped-def]
    """Insert a signed-in browser session and return the raw cookie value."""
    import hashlib

    from app.db.schema import UserSession

    raw = secrets.token_urlsafe(32)

    async def _add() -> None:
        async with factory() as db:
            db.add(UserSession(id_hash=hashlib.sha256(raw.encode()).hexdigest(), upn=upn, oid=upn, name=upn,
                               roles=roles, csrf="csrf-token", created_at=datetime.now(UTC),
                               expires_at=datetime.now(UTC) + timedelta(hours=1)))
            await db.commit()

    asyncio.run(_add())
    return raw


@pytest.fixture
def app_client(db_factory):  # type: ignore[no-untyped-def]
    """Unauthenticated TestClient over a fresh database (startup DB init skipped)."""
    from app.main import app

    async def _noop_init_db(database_url: str | None = None) -> None:
        pass

    with (
        mock.patch("app.main.init_db", side_effect=_noop_init_db),
        TestClient(app, raise_server_exceptions=True) as client,
    ):
        yield client


@pytest.fixture
def test_client(app_client, db_factory):  # type: ignore[no-untyped-def]
    """TestClient signed in as an admin (every role), for API tests that are not about auth."""
    app_client.cookies.set("msp_session", make_session(db_factory, ADMIN_ROLES, "admin@example.com"))
    yield app_client
