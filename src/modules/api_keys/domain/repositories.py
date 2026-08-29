"""API key reads — every ``select(...)`` for this module lives here.

``ApiKeyRepository`` is tenant-scoped for everything a signed-in user does. ``authenticate`` is the
deliberate exception and is spelled out below.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.api_keys.domain.models import ApiKey
from src.shared.database.pagination import Page, PageRequest
from src.shared.database.repository import TenantScopedRepository


class ApiKeyRepository(TenantScopedRepository[ApiKey]):
    model = ApiKey

    async def list_for_agent(self, agent_id: uuid.UUID, page: PageRequest) -> Page[ApiKey]:
        query = self._base_query().where(ApiKey.agent_id == agent_id)

        total = (
            await self.session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = await self.session.execute(
            query.order_by(ApiKey.created_at.desc()).offset(page.offset).limit(page.limit)
        )
        return Page(
            items=list(rows.scalars().all()),
            total=total,
            page=page.page,
            page_size=page.page_size,
        )

    async def active_count_for_agent(self, agent_id: uuid.UUID) -> int:
        query = select(func.count()).select_from(
            self._base_query()
            .where(ApiKey.agent_id == agent_id, ApiKey.revoked_at.is_(None))
            .subquery()
        )
        return int((await self.session.execute(query)).scalar_one())


async def authenticate(session: AsyncSession, key_hash: str) -> ApiKey | None:
    """Find a key by its hash, across every tenant.

    **The one unscoped read in the codebase, and it has to be.** Authentication is what *determines*
    the tenant — there is no tenant to scope by until this returns. It is a module-level function
    rather than a method on the scoped repository so it cannot be reached by accident through an
    object a request already holds, and it returns the row rather than any decision: whether the key
    is revoked, expired, or carries the right scope is the service's call, not a query's.

    The lookup is by exact hash against a unique index, so a caller cannot use it to enumerate
    anything — they must already hold the secret.
    """
    query = select(ApiKey).where(ApiKey.key_hash == key_hash)
    return (await session.execute(query)).scalar_one_or_none()
