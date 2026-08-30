"""Cross-tenant reads for platform staff — every ``select(...)`` this module makes lives here.

**This is the one module in the codebase that reads across tenants, and that is the point.**
Everywhere else, a query that could see two tenants' rows is a bug (spec §5.7); here it is the
feature. So the exception is confined to this file, named as such, and shaped to be as small as it
can be:

* Only ``tenant``, and counts of the rows that hang off one. There is no cross-tenant read of an
  agent's configuration, a conversation's transcript, or a knowledge base's contents — an admin
  reaches those by *acting as* the tenant through the ordinary endpoints, where the usual
  tenant-scoped repositories still do the work (see ``tenants/presentation/dependencies.py``).
* Reads only. The one write an admin makes to a tenant row goes through ``TenantService``, which is
  the module that owns it.

The result is that "what can platform staff see that nobody else can?" has a short answer: the list
of accounts and how big each one is. Everything else they do, they do as a tenant, through code that
was already scoped.

``BaseRepository`` rather than ``TenantScopedRepository`` is therefore deliberate here and nowhere
else, and the architecture test records it as a sanctioned exception.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Select, func, select

from src.modules.agents.domain.models import Agent
from src.modules.conversations.domain.models import Conversation, Message
from src.modules.knowledge_base.domain.models import KbSource
from src.modules.tenants.domain.models import Tenant, TenantStatus, User
from src.shared.database.pagination import Page, PageRequest
from src.shared.database.repository import BaseRepository


@dataclass(frozen=True, slots=True)
class TenantCounts:
    """How big one account is, without looking inside it."""

    users: int
    agents: int
    conversations: int
    messages: int
    stored_bytes: int


@dataclass(frozen=True, slots=True)
class PlatformTotals:
    """The whole deployment in one row."""

    tenants: int
    active_tenants: int
    disabled_tenants: int
    users: int
    agents: int
    conversations: int


class TenantDirectoryRepository(BaseRepository[Tenant]):
    """The account list and the counts beside it."""

    model = Tenant

    async def search(
        self, page: PageRequest, term: str | None = None, status: TenantStatus | None = None
    ) -> Page[Tenant]:
        query: Select[tuple[Tenant]] = select(Tenant)
        if term and term.strip():
            # Case-insensitive substring on the name. Enough for an operator looking for "acme";
            # a deployment with tens of thousands of accounts wants a real index, and will say so.
            query = query.where(Tenant.name.ilike(f"%{term.strip()}%"))
        if status is not None:
            query = query.where(Tenant.status == status)

        total = (
            await self.session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = await self.session.execute(
            query.order_by(Tenant.created_at.desc()).offset(page.offset).limit(page.limit)
        )
        return Page(
            items=list(rows.scalars().all()),
            total=int(total),
            page=page.page,
            page_size=page.page_size,
        )

    async def counts_for(self, tenant_id: uuid.UUID) -> TenantCounts:
        """The five numbers the console shows per account.

        Counted per tenant rather than as one grouped query over every tenant: the list endpoint
        asks for them one at a time and a page holds twenty, which is twenty cheap indexed counts
        against one join over the largest table in the schema.
        """
        users = await self._count(select(User.id).where(User.tenant_id == tenant_id))
        agents = await self._count(select(Agent.id).where(Agent.tenant_id == tenant_id))
        conversations = await self._count(
            select(Conversation.id).where(Conversation.tenant_id == tenant_id)
        )
        messages = await self._count(
            select(Message.id)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.tenant_id == tenant_id)
        )
        stored = (
            await self.session.execute(
                select(func.coalesce(func.sum(KbSource.byte_size), 0)).where(
                    KbSource.tenant_id == tenant_id
                )
            )
        ).scalar_one()

        return TenantCounts(
            users=users,
            agents=agents,
            conversations=conversations,
            messages=messages,
            stored_bytes=int(stored),
        )

    async def totals(self) -> PlatformTotals:
        tenants = await self._count(select(Tenant.id))
        disabled = await self._count(
            select(Tenant.id).where(Tenant.status == TenantStatus.DISABLED)
        )
        return PlatformTotals(
            tenants=tenants,
            active_tenants=tenants - disabled,
            disabled_tenants=disabled,
            users=await self._count(select(User.id)),
            agents=await self._count(select(Agent.id)),
            conversations=await self._count(select(Conversation.id)),
        )

    async def _count(self, query: Select[tuple[uuid.UUID]]) -> int:
        total = await self.session.execute(select(func.count()).select_from(query.subquery()))
        return int(total.scalar_one())


class PlatformUserRepository(BaseRepository[User]):
    """Users, across tenants — for finding the account behind an email address.

    An operator's most common starting point is a person, not a tenant: somebody writes in, and the
    only thing they can be identified by is the address they signed up with.
    """

    model = User

    async def find_by_email(self, email: str) -> User | None:
        query = select(User).where(func.lower(User.email) == email.strip().lower())
        return (await self.session.execute(query)).scalar_one_or_none()

    async def list_for_tenant(self, tenant_id: uuid.UUID) -> list[User]:
        query = select(User).where(User.tenant_id == tenant_id).order_by(User.created_at)
        return list((await self.session.execute(query)).scalars().all())
