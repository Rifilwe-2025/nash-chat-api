"""Knowledge base reads — every ``select(...)`` for this module lives here.

``KnowledgeBaseRepository`` and ``KbSourceRepository`` are both tenant-scoped, so neither can return
another tenant's rows however they are called. ``AgentKbLinkRepository`` is not: a link is only ever
written or read with an ``agent_id`` and ``kb_id`` that were themselves loaded through a scoped
repository, and scoping the join table as well would add a column that no query needs.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, select

from src.modules.knowledge_base.domain.models import (
    AgentKbLink,
    KbSource,
    KnowledgeBase,
    SourceStatus,
)
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

    async def all_for_agent(self, agent_id: uuid.UUID) -> list[KnowledgeBase]:
        """Every knowledge base attached to an agent — what a retrieval for that agent searches."""
        query = self._base_query().where(
            KnowledgeBase.id.in_(select(AgentKbLink.kb_id).where(AgentKbLink.agent_id == agent_id))
        )
        return list((await self.session.execute(query)).scalars().all())

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

    async def ready_for_kbs(self, kb_ids: Sequence[uuid.UUID]) -> list[KbSource]:
        """Every source with usable text across several knowledge bases — Tier 1's input.

        An agent may draw on more than one knowledge base, so retrieval reads them together rather
        than one at a time. Sources that failed or are still processing are excluded here: they have
        no text, and there is nothing to inject.
        """
        if not kb_ids:
            return []
        query = (
            self._base_query()
            .where(
                KbSource.kb_id.in_(kb_ids),
                KbSource.status == SourceStatus.READY,
                KbSource.extracted_text.is_not(None),
            )
            .order_by(KbSource.created_at)
        )
        return list((await self.session.execute(query)).scalars().all())

    async def total_characters(self, kb_ids: Sequence[uuid.UUID]) -> int:
        """How much text these knowledge bases hold — what the tier router weighs.

        Measured in the database rather than by loading every source: the whole reason to ask is
        that the content may be too big to hold in memory, so the question must not require it.
        """
        if not kb_ids:
            return 0
        query = select(func.coalesce(func.sum(func.length(KbSource.extracted_text)), 0)).where(
            KbSource.tenant_id == self.tenant_id,
            KbSource.kb_id.in_(kb_ids),
            KbSource.status == SourceStatus.READY,
        )
        return int((await self.session.execute(query)).scalar_one())

    async def search(
        self, kb_ids: Sequence[uuid.UUID], query_text: str, limit: int
    ) -> list[tuple[KbSource, float, str]]:
        """Tier 2: Postgres full-text search, ranked, with the matching passage cut out.

        ``websearch_to_tsquery`` rather than ``plainto_tsquery`` because tenants' end users type
        like they type into a search box — quoted phrases and ``or`` should mean what they look
        like, and unlike ``to_tsquery`` it cannot be made to raise on punctuation.

        ``ts_headline`` is what makes top-N *section* selection possible without storing sections:
        Postgres finds the relevant fragments inside the stored text at query time. Nothing is
        chunked, and nothing is embedded (spec §5.2.2 — Tier 3 is v2).
        """
        if not kb_ids or not query_text.strip():
            return []

        tsquery = func.websearch_to_tsquery("english", query_text)
        rank = func.ts_rank_cd(KbSource.search_vector, tsquery).label("rank")
        headline = func.ts_headline(
            "english",
            func.coalesce(KbSource.extracted_text, ""),
            tsquery,
            'StartSel="", StopSel="", MaxFragments=3, MinWords=10, MaxWords=40, '
            'FragmentDelimiter=" … "',
        ).label("headline")

        statement = (
            select(KbSource, rank, headline)
            .where(
                KbSource.tenant_id == self.tenant_id,
                KbSource.kb_id.in_(kb_ids),
                KbSource.status == SourceStatus.READY,
                KbSource.search_vector.op("@@")(tsquery),
            )
            .order_by(rank.desc(), KbSource.created_at)
            .limit(limit)
        )

        rows = await self.session.execute(statement)
        return [(row[0], float(row[1]), row[2]) for row in rows.all()]

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
