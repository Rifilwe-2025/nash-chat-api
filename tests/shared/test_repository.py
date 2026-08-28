"""Base repository behaviour, exercised through the tenants module's repositories."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.tenants.domain.models import Tenant, TenantPlan, User
from src.modules.tenants.domain.repositories import TenantRepository, UserRepository
from src.shared.database.pagination import PageRequest

MakeTenant = Callable[..., Coroutine[Any, Any, Tenant]]
MakeUser = Callable[..., Coroutine[Any, Any, User]]


async def test_add_populates_generated_columns(session: AsyncSession) -> None:
    repository = TenantRepository(session)

    tenant = await repository.add(Tenant(name="Acme"))

    assert isinstance(tenant.id, uuid.UUID)
    assert tenant.created_at is not None
    assert tenant.updated_at is not None
    assert tenant.plan is TenantPlan.FREE


async def test_get_returns_none_for_unknown_id(session: AsyncSession) -> None:
    repository = TenantRepository(session)

    assert await repository.get(uuid.uuid4()) is None


async def test_repository_never_commits(session: AsyncSession, make_tenant: MakeTenant) -> None:
    """The session dependency owns the transaction; add() must only flush."""
    await make_tenant(name="Uncommitted")

    assert session.in_transaction()


async def test_list_paginates_and_counts(session: AsyncSession, make_tenant: MakeTenant) -> None:
    for index in range(5):
        await make_tenant(name=f"Tenant {index}")
    repository = TenantRepository(session)

    first = await repository.list(PageRequest(page=1, page_size=2))
    second = await repository.list(PageRequest(page=2, page_size=2))

    assert first.total == 5
    assert len(first.items) == 2
    assert len(second.items) == 2
    assert {t.id for t in first.items}.isdisjoint({t.id for t in second.items})


async def test_update_changes_fields(session: AsyncSession, make_tenant: MakeTenant) -> None:
    tenant = await make_tenant(name="Before")
    repository = TenantRepository(session)

    updated = await repository.update(tenant, name="After", plan=TenantPlan.PRO)

    assert updated.name == "After"
    assert updated.plan is TenantPlan.PRO


async def test_delete_removes_the_row(session: AsyncSession, make_tenant: MakeTenant) -> None:
    tenant = await make_tenant()
    repository = TenantRepository(session)

    await repository.delete(tenant)

    assert await repository.get(tenant.id) is None


async def test_exists_reflects_presence(session: AsyncSession, make_tenant: MakeTenant) -> None:
    tenant = await make_tenant()
    repository = TenantRepository(session)

    assert await repository.exists(tenant.id) is True
    assert await repository.exists(uuid.uuid4()) is False


async def test_user_lookup_by_email_is_case_insensitive(
    session: AsyncSession, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant = await make_tenant()
    await make_user(tenant, email="Owner@Example.com")
    repository = UserRepository(session)

    found = await repository.get_by_email("owner@example.com")

    assert found is not None
    assert found.tenant_id == tenant.id
    assert await repository.email_exists("OWNER@EXAMPLE.COM") is True


async def test_users_are_listed_per_tenant(
    session: AsyncSession, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    first = await make_tenant(name="First")
    second = await make_tenant(name="Second")
    await make_user(first)
    await make_user(first)
    await make_user(second)
    repository = UserRepository(session)

    assert len(await repository.list_for_tenant(first.id)) == 2
    assert len(await repository.list_for_tenant(second.id)) == 1
