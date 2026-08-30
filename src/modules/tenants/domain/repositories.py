"""Tenant and user repositories — every ``select(...)`` for this module lives here."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from src.modules.tenants.domain.models import Tenant, User
from src.shared.database.repository import BaseRepository


class TenantRepository(BaseRepository[Tenant]):
    model = Tenant


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        query = self._base_query().where(func.lower(User.email) == email.lower())
        return (await self.session.execute(query)).scalar_one_or_none()

    async def get_with_tenant(self, user_id: uuid.UUID) -> User | None:
        """A user with their tenant already loaded.

        Every authenticated request has to know whether the account is still enabled, and a lazy
        relationship cannot answer that under asyncio — it would raise rather than emit a query.
        A ``joinedload`` answers it in the same round trip the user is fetched in, which is why
        the authentication path uses this rather than the plain ``get``.
        """
        query = self._base_query().where(User.id == user_id).options(joinedload(User.tenant))
        return (await self.session.execute(query)).unique().scalar_one_or_none()

    async def get_by_email_with_tenant(self, email: str) -> User | None:
        query = (
            self._base_query()
            .where(func.lower(User.email) == email.lower())
            .options(joinedload(User.tenant))
        )
        return (await self.session.execute(query)).unique().scalar_one_or_none()

    async def email_exists(self, email: str) -> bool:
        query = select(func.count()).select_from(
            select(User.id).where(func.lower(User.email) == email.lower()).subquery()
        )
        return (await self.session.execute(query)).scalar_one() > 0

    async def list_for_tenant(self, tenant_id: uuid.UUID) -> list[User]:
        query = self._base_query().where(User.tenant_id == tenant_id).order_by(User.created_at)
        return list((await self.session.execute(query)).scalars().all())
