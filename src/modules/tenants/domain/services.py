"""Account and tenant business logic.

Every read here is scoped by the caller's tenant id, which arrives from the request dependency and
is never taken from the request body — that is what stops a caller naming someone else's tenant.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.tenants.domain.models import Tenant, User
from src.modules.tenants.domain.repositories import TenantRepository, UserRepository
from src.shared.exceptions import ConflictException, NotFoundException


class TenantService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.tenants = TenantRepository(session)
        self.users = UserRepository(session)

    async def get_tenant(self, tenant_id: uuid.UUID) -> Tenant:
        tenant = await self.tenants.get(tenant_id)
        if tenant is None:
            raise NotFoundException("Tenant does not exist.", code="TENANT_NOT_FOUND")
        return tenant

    async def rename_tenant(self, tenant_id: uuid.UUID, name: str) -> Tenant:
        tenant = await self.get_tenant(tenant_id)
        return await self.tenants.update(tenant, name=name)

    async def list_members(self, tenant_id: uuid.UUID) -> list[User]:
        return await self.users.list_for_tenant(tenant_id)

    async def update_profile(
        self,
        user: User,
        full_name: str | None = None,
        email: str | None = None,
    ) -> User:
        changes: dict[str, object] = {}
        if full_name is not None:
            changes["full_name"] = full_name
        if email is not None and email.lower() != user.email:
            if await self.users.email_exists(email):
                raise ConflictException(
                    "An account with that email already exists.", code="EMAIL_TAKEN"
                )
            changes["email"] = email.lower()
        if not changes:
            return user
        return await self.users.update(user, **changes)
