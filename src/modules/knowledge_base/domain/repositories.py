"""Knowledge base reads — every ``select(...)`` for this module lives here.

``KnowledgeBaseRepository`` and ``KbSourceRepository`` are both tenant-scoped, so neither can return
another tenant's rows however they are called. ``AgentKbLinkRepository`` is not: a link is only ever
written or read with an ``agent_id`` and ``kb_id`` that were themselves loaded through a scoped
repository, and scoping the join table as well would add a column that no query needs.
"""

from __future__ import annotations

import re
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

_TERM = re.compile(r"[\w.]+")
# ``websearch_to_tsquery`` reads these as operators; passing them back through as search terms
# would rebuild the conjunction this widening exists to escape.
_OPERATORS = frozenset({"or", "and", "not"})
_MAX_WIDENED_TERMS = 24


def _share_of(limit: int, knowledge_bases: int) -> int:
    """How many passages one knowledge base may contribute.

    An even split, rounded up, so nothing is starved and a single knowledge base still gets the
    whole budget when it is the only one attached. Rounding up matters at small limits: five
    slots across two knowledge bases is three and three, not two and two, because the caller
    asked for five and truncating would hand back four.
    """
    if knowledge_bases <= 1:
        return limit
    return -(-limit // knowledge_bases)


def widen_query(query_text: str) -> str | None:
    """Rewrite a question as an OR of its terms, or ``None`` if that would change nothing.

    ``websearch_to_tsquery`` conjoins every non-stopword term, so one word that appears in no
    document empties the result set rather than merely ranking it lower. That is right for a
    precise phrase and wrong for how people actually write: "Hi, how much is 20L white PVA?"
    fails on *hi* and *much*, and a request naming two products can never be satisfied by one
    document at all.

    Widening keeps ``websearch_to_tsquery`` rather than reaching for ``to_tsquery`` — the same
    reason the narrow pass uses it, that it cannot be made to raise on punctuation.
    """
    terms = [
        term
        for term in _TERM.findall(query_text.lower())
        if term not in _OPERATORS and any(char.isalnum() for char in term)
    ]
    unique = list(dict.fromkeys(terms))[:_MAX_WIDENED_TERMS]
    if len(unique) < 2:
        return None
    return " or ".join(unique)


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

        Two passes. The first takes the question as written, which is precise and is what answers
        a well-aimed query. When it matches nothing the question is retried with its terms ORed,
        because an empty result set here is indistinguishable to the agent from "we do not stock
        that" — it is handed the no-context note and gives its fallback response. Widening only
        runs where the narrow pass already returned nothing, so it cannot dilute a query that
        worked; ``ts_rank_cd`` ordering and the caller's ``min_rank`` floor still decide what is
        worth injecting.
        """
        rows = await self._search(kb_ids, query_text, limit)
        if rows:
            return rows

        widened = widen_query(query_text)
        if widened is None:
            return []
        return await self._search(kb_ids, widened, limit)

    async def _search(
        self, kb_ids: Sequence[uuid.UUID], query_text: str, limit: int
    ) -> list[tuple[KbSource, float, str]]:
        """One full-text pass. See ``search`` for why there are two.

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

        # Rank within each knowledge base before taking the overall best, so a large body of
        # knowledge cannot starve a small one. ``ts_rank_cd`` accumulates with document length:
        # a long datasheet that mentions a word fifty times outscores a short price list that
        # answers the question exactly, and with a flat limit it takes every slot. An agent's
        # answer "should be able to come from any of them" (see ``retrieve``), which a global
        # top-N quietly stops being true the moment one knowledge base is much bigger.
        per_kb_rank = func.row_number().over(
            partition_by=KbSource.kb_id,
            order_by=(rank.desc(), KbSource.created_at),
        )
        ranked = (
            select(
                KbSource.id.label("source_id"),
                rank,
                headline,
                per_kb_rank.label("rank_within_kb"),
            )
            .where(
                KbSource.tenant_id == self.tenant_id,
                KbSource.kb_id.in_(kb_ids),
                KbSource.status == SourceStatus.READY,
                KbSource.search_vector.op("@@")(tsquery),
            )
            .subquery()
        )

        statement = (
            select(KbSource, ranked.c.rank, ranked.c.headline)
            .join(ranked, KbSource.id == ranked.c.source_id)
            .where(ranked.c.rank_within_kb <= _share_of(limit, len(kb_ids)))
            .order_by(ranked.c.rank.desc(), KbSource.created_at)
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
