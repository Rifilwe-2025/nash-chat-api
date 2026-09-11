"""Personal access token reads — every ``select(...)`` for this module lives here.

``PersonalAccessTokenRepository`` is tenant-scoped for everything a signed-in user does with their
own tokens. ``authenticate`` is the deliberate exception, for the same reason the API key module has
one: it is what *establishes* the tenant, so there is nothing to scope by until it returns.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mcp.domain.models import PersonalAccessToken
from src.shared.database.pagination import Page, PageRequest
from src.shared.database.repository import TenantScopedRepository


class PersonalAccessTokenRepository(TenantScopedRepository[PersonalAccessToken]):
    model = PersonalAccessToken

    async def list_for_user(
        self, user_id: uuid.UUID, page: PageRequest
    ) -> Page[PersonalAccessToken]:
        query = self._base_query().where(PersonalAccessToken.user_id == user_id)

        total = (
            await self.session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = await self.session.execute(
            query.order_by(PersonalAccessToken.created_at.desc())
            .offset(page.offset)
            .limit(page.limit)
        )
        return Page(
            items=list(rows.scalars().all()),
            total=total,
            page=page.page,
            page_size=page.page_size,
        )


async def authenticate(session: AsyncSession, token_hash: str) -> PersonalAccessToken | None:
    """Find a token by its hash, across every tenant.

    Unscoped because it has to be — see ``authenticate`` in the API key module's repositories for
    the full argument. The lookup is an exact match on a unique index, so it cannot enumerate
    anything: the caller must already hold the secret. It returns the row rather than a decision;
    whether the token is revoked, expired, or belongs to a disabled account is the service's call.
    """
    query = select(PersonalAccessToken).where(PersonalAccessToken.token_hash == token_hash)
    return (await session.execute(query)).scalar_one_or_none()
