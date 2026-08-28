"""Knowledge base reads — every ``select(...)`` for this module lives here.

``KnowledgeBaseRepository`` and ``KbSourceRepository`` are both tenant-scoped, so neither can return
another tenant's rows however they are called. ``AgentKbLinkRepository`` is not: a link is only ever
written or read with an ``agent_id`` and ``kb_id`` that were themselves loaded through a scoped
repository, and scoping the join table as well would add a column that no query needs.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, select

from src.modules.knowledge_base.domain.models import AgentKbLink, KbSource, KnowledgeBase
from src.shared.database.pagination import Page, PageRequest
from src.shared.database.repository import BaseRepository, TenantScopedRepository


class KnowledgeBaseRepository(TenantScopedRepository[KnowledgeBase]):
    model = KnowledgeBase

    async def name_taken(self, name: str, exclude_id: uuid.UUID | None = None) -> bool:
        query = self._base_query().where(func.lower(KnowledgeBase.name) == name.lower())
        if exclude_id is not None:
            query = query.where(KnowledgeBase.id != exclude_id)

        count_query = select(func.count()).select_from(query.subquery())
        return (await self.session.execute(count_query)).scalar_one() > 0

    async def list_for_agent(self, agent_id: uuid.UUID, page: PageRequest) -> Page[KnowledgeBase]:
        """The knowledge bases attached to one agent, newest first.

        Still built on the tenant-scoped base query, so an agent id from another tenant returns
        nothing rather than that tenant's knowledge bases.
        """
        query = self._base_query().where(
            KnowledgeBase.id.in_(select(AgentKbLink.kb_id).where(AgentKbLink.agent_id == agent_id))
        )
        return await self._paginate(query, page)

    async def _paginate(
        self, query: Select[tuple[KnowledgeBase]], page: PageRequest
    ) -> Page[KnowledgeBase]:
        total = (
            await self.session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = await self.session.execute(
            query.order_by(KnowledgeBase.created_at.desc()).offset(page.offset).limit(page.limit)
        )
        return Page(
            items=list(rows.scalars().all()),
            total=total,
            page=page.page,
            page_size=page.page_size,
        )


class KbSourceRepository(TenantScopedRepository[KbSource]):
    model = KbSource

    async def list_for_kb(self, kb_id: uuid.UUID, page: PageRequest) -> Page[KbSource]:
        query = self._base_query().where(KbSource.kb_id == kb_id)

        total = (
            await self.session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = await self.session.execute(
            query.order_by(KbSource.created_at.desc()).offset(page.offset).limit(page.limit)
        )
        return Page(
            items=list(rows.scalars().all()),
            total=total,
            page=page.page,
            page_size=page.page_size,
        )

    async def all_for_kb(self, kb_id: uuid.UUID) -> list[KbSource]:
        """Every source in a knowledge base, oldest first — what Phase 6 assembles for Tier 1."""
        query = self._base_query().where(KbSource.kb_id == kb_id).order_by(KbSource.created_at)
        return list((await self.session.execute(query)).scalars().all())

    async def count_for_kb(self, kb_id: uuid.UUID) -> int:
        query = select(func.count()).select_from(
            self._base_query().where(KbSource.kb_id == kb_id).subquery()
        )
        return int((await self.session.execute(query)).scalar_one())

    async def total_bytes(self) -> int:
        """Storage used by this tenant, across every knowledge base it owns."""
        query = select(func.coalesce(func.sum(KbSource.byte_size), 0)).where(
            KbSource.tenant_id == self.tenant_id
        )
        return int((await self.session.execute(query)).scalar_one())


class AgentKbLinkRepository(BaseRepository[AgentKbLink]):
    model = AgentKbLink

    async def get_link(self, agent_id: uuid.UUID, kb_id: uuid.UUID) -> AgentKbLink | None:
        query = self._base_query().where(
            AgentKbLink.agent_id == agent_id, AgentKbLink.kb_id == kb_id
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def agent_ids_for_kb(self, kb_id: uuid.UUID) -> list[uuid.UUID]:
        query = (
            select(AgentKbLink.agent_id)
            .where(AgentKbLink.kb_id == kb_id)
            .order_by(AgentKbLink.created_at)
        )
        return list((await self.session.execute(query)).scalars().all())

    async def count_for_kb(self, kb_id: uuid.UUID) -> int:
        query = select(func.count()).select_from(
            select(AgentKbLink.id).where(AgentKbLink.kb_id == kb_id).subquery()
        )
        return int((await self.session.execute(query)).scalar_one())
