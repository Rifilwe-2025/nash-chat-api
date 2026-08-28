"""Schema guarantees that later phases depend on."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.tenants.domain.models import Tenant, User, UserRole

MakeTenant = Callable[..., Coroutine[Any, Any, Tenant]]
MakeUser = Callable[..., Coroutine[Any, Any, User]]


async def test_email_is_unique(
    session: AsyncSession, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant = await make_tenant()
    await make_user(tenant, email="duplicate@example.com")

    with pytest.raises(IntegrityError):
        await make_user(tenant, email="duplicate@example.com")


async def test_user_requires_a_tenant(session: AsyncSession) -> None:
    session.add(User(tenant_id=None, email="orphan@example.com", role=UserRole.OWNER))

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_deleting_a_tenant_cascades_to_users(
    session: AsyncSession, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant = await make_tenant()
    await make_user(tenant)

    await session.delete(tenant)
    await session.flush()

    remaining = await session.execute(select(User).where(User.tenant_id == tenant.id))
    assert remaining.scalars().all() == []


async def test_timestamps_are_timezone_aware(
    session: AsyncSession, make_tenant: MakeTenant
) -> None:
    tenant = await make_tenant()

    assert tenant.created_at.tzinfo is not None
    assert tenant.updated_at.tzinfo is not None


async def test_migration_created_the_expected_tables(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
    )
    tables = {row[0] for row in rows}

    assert {"alembic_version", "tenant", "user"} <= tables
