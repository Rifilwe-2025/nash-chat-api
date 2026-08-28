"""Test harness.

The suite runs against a real Postgres — the isolation and constraint behaviour we care about
cannot be exercised on SQLite. A dedicated database (``DATABASE_TEST_URL``) is created once per
session and migrated with Alembic, so the migrations themselves are covered by every run. Each test
then runs inside a transaction that is rolled back, so tests never see each other's rows.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from src import configs
from src.core.factory import create_app
from src.modules.tenants.domain.models import Tenant, TenantPlan, User, UserRole
from src.shared.database.dependencies import get_session
from src.shared.database.engine import create_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def _ensure_database(url: str) -> None:
    """Create the test database if it does not exist.

    `CREATE DATABASE` can be issued from any database, so the configured application database is
    used as the maintenance connection rather than `postgres` — a local `pg_hba.conf` may well
    grant access to one and not the other.
    """
    database_name = make_url(url).database
    assert database_name, "DATABASE_TEST_URL must name a database"

    maintenance_url = configs.DATABASE_URL
    if make_url(maintenance_url).database == database_name:
        raise RuntimeError(
            "DATABASE_TEST_URL must not point at the application database — "
            "the harness drops and recreates its schema."
        )

    admin_engine = create_engine(maintenance_url)
    try:
        async with admin_engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(isolation_level="AUTOCOMMIT")
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": database_name},
            )
            if not exists:
                await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    finally:
        await admin_engine.dispose()


def _alembic_config(url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture(scope="session")
def database_url() -> str:
    url: str = configs.DATABASE_TEST_URL
    return url


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    """Create and migrate the test database once per session.

    Deliberately synchronous: a session-scoped *async* fixture would need a session-scoped event
    loop, and asyncpg connections cannot then be shared with the per-test loops. Alembic runs its
    own loop internally, so the expensive work still happens exactly once.
    """
    asyncio.run(_ensure_database(database_url))

    config = _alembic_config(database_url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    return database_url


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    """Per-test engine, so every connection belongs to the running test's event loop."""
    test_engine = create_engine(migrated_database)
    yield test_engine
    await test_engine.dispose()


@pytest.fixture
async def connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """One outer transaction per test, always rolled back."""
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()


@pytest.fixture
async def session(connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """Session bound to the test transaction; its commits become nested savepoints."""
    factory = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    async with factory() as db_session:
        yield db_session


@pytest.fixture
async def client(connection: AsyncConnection, engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """App client whose request sessions join the test transaction, so requests roll back too."""
    app = create_app()
    app.state.engine = engine

    factory = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    app.state.session_factory = factory

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with factory() as db_session:
            yield db_session

    app.dependency_overrides[get_session] = override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client

    app.dependency_overrides.clear()


@pytest.fixture
def make_tenant(session: AsyncSession) -> Callable[..., Coroutine[Any, Any, Tenant]]:
    async def _make(name: str = "Acme", plan: TenantPlan = TenantPlan.FREE) -> Tenant:
        tenant = Tenant(name=name, plan=plan)
        session.add(tenant)
        await session.flush()
        return tenant

    return _make


@pytest.fixture
def make_user(session: AsyncSession) -> Callable[..., Coroutine[Any, Any, User]]:
    async def _make(
        tenant: Tenant,
        email: str | None = None,
        role: UserRole = UserRole.OWNER,
        full_name: str | None = None,
    ) -> User:
        user = User(
            tenant_id=tenant.id,
            email=email or f"user-{uuid.uuid4().hex[:8]}@example.com",
            role=role,
            full_name=full_name,
        )
        session.add(user)
        await session.flush()
        return user

    return _make
