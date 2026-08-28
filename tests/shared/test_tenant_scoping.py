"""The tenant-scoping layer (spec §5.7).

These tests exercise the repository directly rather than through HTTP: the guarantee being made is
that the *query layer* cannot reach across tenants, so it must hold no matter which endpoint,
service, or future module calls it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.tenants.domain.models import Tenant, User
from src.shared.database.pagination import PageRequest
from src.shared.database.repository import CrossTenantAccessError, TenantScopedRepository

MakeTenant = Callable[..., Coroutine[Any, Any, Tenant]]
MakeUser = Callable[..., Coroutine[Any, Any, User]]


class ScopedUsers(TenantScopedRepository[User]):
    model = User


async def test_get_cannot_reach_another_tenants_row(
    session: AsyncSession, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant_a = await make_tenant(name="A")
    tenant_b = await make_tenant(name="B")
    user_a = await make_user(tenant_a)

    as_b = ScopedUsers(session, tenant_b.id)

    assert await as_b.get(user_a.id) is None
    assert await as_b.exists(user_a.id) is False


async def test_list_only_returns_the_callers_rows(
    session: AsyncSession, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant_a = await make_tenant(name="A")
    tenant_b = await make_tenant(name="B")
    await make_user(tenant_a)
    await make_user(tenant_a)
    user_b = await make_user(tenant_b)

    page = await ScopedUsers(session, tenant_b.id).list(PageRequest(page=1, page_size=50))

    assert page.total == 1
    assert [row.id for row in page.items] == [user_b.id]


async def test_delete_by_id_cannot_remove_another_tenants_row(
    session: AsyncSession, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    """The bulk delete bypasses `_base_query`, so it re-applies the scope itself."""
    tenant_a = await make_tenant(name="A")
    tenant_b = await make_tenant(name="B")
    user_a = await make_user(tenant_a)

    removed = await ScopedUsers(session, tenant_b.id).delete_by_id(user_a.id)

    assert removed == 0
    assert await ScopedUsers(session, tenant_a.id).get(user_a.id) is not None


async def test_update_rejects_an_entity_from_another_tenant(
    session: AsyncSession, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant_a = await make_tenant(name="A")
    tenant_b = await make_tenant(name="B")
    user_a = await make_user(tenant_a)

    with pytest.raises(CrossTenantAccessError):
        await ScopedUsers(session, tenant_b.id).update(user_a, full_name="hijacked")


async def test_delete_rejects_an_entity_from_another_tenant(
    session: AsyncSession, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant_a = await make_tenant(name="A")
    tenant_b = await make_tenant(name="B")
    user_a = await make_user(tenant_a)

    with pytest.raises(CrossTenantAccessError):
        await ScopedUsers(session, tenant_b.id).delete(user_a)


async def test_update_cannot_move_a_row_to_another_tenant(
    session: AsyncSession, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant_a = await make_tenant(name="A")
    tenant_b = await make_tenant(name="B")
    user_a = await make_user(tenant_a)

    updated = await ScopedUsers(session, tenant_a.id).update(
        user_a, full_name="Renamed", tenant_id=tenant_b.id
    )

    assert updated.tenant_id == tenant_a.id
    assert updated.full_name == "Renamed"


async def test_add_stamps_the_repositorys_tenant(
    session: AsyncSession, make_tenant: MakeTenant
) -> None:
    """Even a caller who supplies someone else's tenant id writes into their own."""
    tenant_a = await make_tenant(name="A")
    tenant_b = await make_tenant(name="B")

    created = await ScopedUsers(session, tenant_a.id).add(
        User(tenant_id=tenant_b.id, email=f"{uuid.uuid4().hex[:8]}@example.com")
    )

    assert created.tenant_id == tenant_a.id


async def test_scoped_repository_requires_a_tenant_id(session: AsyncSession) -> None:
    with pytest.raises(TypeError):
        ScopedUsers(session)  # type: ignore[call-arg]
