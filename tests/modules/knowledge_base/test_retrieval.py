"""Retrieval end to end, through both tiers (spec §5.2.2).

The phase's bar lives here: a small knowledge base round-trips through Tier 1, a large one through
Tier 2, and an off-topic query returns the no-context signal rather than injecting noise.

These run against real Postgres full-text search rather than a stubbed one — `tsvector` generation,
`websearch_to_tsquery` parsing, `ts_rank_cd` ordering, and `ts_headline` fragment extraction are the
substance of Tier 2, and a fake would test nothing that can actually be wrong.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.agents.domain.services import AgentService
from src.modules.knowledge_base.domain.models import KnowledgeBase, RetrievalTier
from src.modules.knowledge_base.domain.services import KnowledgeBaseService
from src.modules.knowledge_base.internal.retrieval import NoContextReason
from src.modules.tenants.domain.models import Tenant
from src.shared.exceptions import ValidationException

RETURNS = (
    "Paint may be returned within 30 days of purchase with a receipt. "
    "Tinted paint is mixed to order and is final sale, so it cannot be returned."
)
DELIVERY = (
    "Delivery is next working day within Harare for orders placed before 2pm. "
    "Deliveries to Bulawayo take two to three working days."
)
COVERAGE = (
    "One litre of matt emulsion covers approximately 12 square metres on a prepared wall. "
    "Two coats are recommended over bare plaster."
)


@pytest.fixture
async def tenant(make_tenant: Callable[..., Coroutine[Any, Any, Tenant]]) -> Tenant:
    return await make_tenant(name="Nash Paints")


@pytest.fixture
def service(session: AsyncSession, tenant: Tenant) -> KnowledgeBaseService:
    return KnowledgeBaseService(session, tenant.id)


async def stocked(
    service: KnowledgeBaseService,
    name: str = "Policies",
    tier: RetrievalTier = RetrievalTier.AUTO,
    padding: int = 0,
) -> KnowledgeBase:
    """A knowledge base with three entries, optionally padded to force it over the budget."""
    knowledge_base = await service.create(name=name, retrieval_tier=tier)
    for title, body in (
        ("Returns", RETURNS),
        ("Delivery", DELIVERY),
        ("Coverage", COVERAGE),
    ):
        await service.add_manual_source(knowledge_base.id, title=title, body=body)
    if padding:
        await service.add_manual_source(
            knowledge_base.id,
            title="Colour chart",
            body=" ".join(
                f"Shade number {index} is available to order." for index in range(padding)
            ),
        )
    return knowledge_base


# -- Tier 1 --------------------------------------------------------------------------


async def test_a_small_knowledge_base_is_injected_whole(
    service: KnowledgeBaseService, config_override: Callable[..., None]
) -> None:
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=100_000)
    knowledge_base = await stocked(service)

    result = await service.retrieve("Can I return tinted paint?", kb_id=knowledge_base.id)

    assert result.tier is RetrievalTier.DIRECT
    assert result.has_context
    assert len(result.passages) == 3, "Tier 1 hands over everything, it does not select"
    assert any("final sale" in passage.text for passage in result.passages)
    assert any("Bulawayo" in passage.text for passage in result.passages)


async def test_every_injected_passage_names_its_source(
    service: KnowledgeBaseService, config_override: Callable[..., None]
) -> None:
    """Citation metadata on every result (spec §5.2) — the first question about a wrong answer."""
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=100_000)
    knowledge_base = await stocked(service)

    result = await service.retrieve("returns", kb_id=knowledge_base.id)

    names = {passage.citation.source_name for passage in result.passages}
    assert names == {"Returns", "Delivery", "Coverage"}
    for passage in result.passages:
        assert passage.citation.kb_id == knowledge_base.id
        assert passage.citation.source_type == "manual"


async def test_a_failed_source_is_never_injected(
    service: KnowledgeBaseService, config_override: Callable[..., None]
) -> None:
    """Injecting an extraction error as though it were knowledge is worse than omitting it."""
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=100_000)
    knowledge_base = await stocked(service)
    await service.add_file_source(
        knowledge_base.id,
        filename="broken.docx",
        data=b"not a docx at all",
        declared_media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
    )

    result = await service.retrieve("returns", kb_id=knowledge_base.id)

    assert len(result.passages) == 3
    assert not any("Word document" in passage.text for passage in result.passages)


async def test_an_empty_knowledge_base_says_so(service: KnowledgeBaseService) -> None:
    knowledge_base = await service.create(name="Nothing yet")

    result = await service.retrieve("anything", kb_id=knowledge_base.id)

    assert not result.has_context
    assert result.no_context_reason is NoContextReason.EMPTY_KNOWLEDGE_BASE


# -- Tier 2 --------------------------------------------------------------------------


async def test_a_large_knowledge_base_is_searched_and_only_matches_come_back(
    service: KnowledgeBaseService, config_override: Callable[..., None]
) -> None:
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=500)
    knowledge_base = await stocked(service, padding=200)

    result = await service.retrieve("Can I return tinted paint?", kb_id=knowledge_base.id)

    assert result.tier is RetrievalTier.KEYWORD
    assert result.has_context
    assert "Returns" in {passage.citation.source_name for passage in result.passages}
    assert "Shade number" not in " ".join(passage.text for passage in result.passages)


async def test_search_results_are_ranked_best_first(
    service: KnowledgeBaseService, config_override: Callable[..., None]
) -> None:
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=500, KB_KEYWORD_MIN_RANK=0.0)
    knowledge_base = await stocked(service, padding=200)

    result = await service.retrieve("delivery to Bulawayo", kb_id=knowledge_base.id)

    assert result.passages[0].citation.source_name == "Delivery"
    scores = [passage.score or 0.0 for passage in result.passages]
    assert scores == sorted(scores, reverse=True)


async def test_a_search_result_is_a_fragment_not_the_whole_document(
    service: KnowledgeBaseService, config_override: Callable[..., None]
) -> None:
    """Tier 2's point: the relevant passage, cut out at query time — no stored chunks."""
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=500)
    knowledge_base = await service.create(name="Long doc")
    long_body = " ".join(f"Filler sentence number {index}." for index in range(400))
    await service.add_manual_source(
        knowledge_base.id,
        title="Handbook",
        body=f"{long_body} Tinted paint is final sale. {long_body}",
    )

    result = await service.retrieve("tinted paint final sale", kb_id=knowledge_base.id)

    assert result.tier is RetrievalTier.KEYWORD
    assert "final sale" in result.passages[0].text
    assert len(result.passages[0].text) < len(long_body)


async def test_an_off_topic_query_returns_the_no_context_signal(
    service: KnowledgeBaseService, config_override: Callable[..., None]
) -> None:
    """The phase's bar. An agent told nothing was found uses its fallback; an agent handed
    irrelevant text answers from it."""
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=500)
    knowledge_base = await stocked(service, padding=200)

    result = await service.retrieve("elephant migration patterns", kb_id=knowledge_base.id)

    assert result.tier is RetrievalTier.KEYWORD
    assert not result.has_context
    assert result.no_context_reason is NoContextReason.NO_MATCH
    assert result.passages == []


async def test_a_weak_match_below_the_threshold_is_treated_as_no_context(
    service: KnowledgeBaseService, config_override: Callable[..., None]
) -> None:
    """A shared common word is not relevance. The threshold is what separates the two."""
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=500, KB_KEYWORD_MIN_RANK=0.99)
    knowledge_base = await stocked(service, padding=200)

    result = await service.retrieve("paint", kb_id=knowledge_base.id)

    assert not result.has_context
    assert result.no_context_reason is NoContextReason.BELOW_THRESHOLD


async def test_the_index_follows_the_text_when_a_source_changes(
    service: KnowledgeBaseService, config_override: Callable[..., None]
) -> None:
    """The search vector is a generated column, so it cannot drift out of step with the text."""
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=500)
    knowledge_base = await stocked(service, padding=200)
    source = next(
        item
        for item in await service.sources.all_for_kb(knowledge_base.id)
        if item.name == "Returns"
    )

    await service.sources.update(source, extracted_text="Refunds are issued as store credit only.")

    assert not (await service.retrieve("tinted final sale", kb_id=knowledge_base.id)).has_context
    refreshed = await service.retrieve("store credit refunds", kb_id=knowledge_base.id)
    assert refreshed.has_context
    assert "store credit" in refreshed.passages[0].text


# -- manual override -------------------------------------------------------------------


async def test_a_small_knowledge_base_pinned_to_keyword_is_searched(
    service: KnowledgeBaseService, config_override: Callable[..., None]
) -> None:
    """A reasonable thing to want: only the relevant paragraph should reach the prompt."""
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=100_000)
    knowledge_base = await stocked(service, tier=RetrievalTier.KEYWORD)

    result = await service.retrieve("tinted paint", kb_id=knowledge_base.id)

    assert result.tier is RetrievalTier.KEYWORD
    assert len(result.passages) == 1


async def test_a_large_knowledge_base_pinned_to_direct_is_still_injected(
    service: KnowledgeBaseService, config_override: Callable[..., None]
) -> None:
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=100)
    knowledge_base = await stocked(service, tier=RetrievalTier.DIRECT, padding=50)

    result = await service.retrieve("anything at all", kb_id=knowledge_base.id)

    assert result.tier is RetrievalTier.DIRECT
    assert len(result.passages) == 4


# -- retrieving for an agent -------------------------------------------------------------


async def test_an_agent_retrieves_across_every_knowledge_base_attached_to_it(
    session: AsyncSession, tenant: Tenant, config_override: Callable[..., None]
) -> None:
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=100_000)
    service = KnowledgeBaseService(session, tenant.id)
    agent = await AgentService(session, tenant.id).create(name="Sales Assistant")
    policies = await service.create(name="Policies")
    await service.add_manual_source(policies.id, title="Returns", body=RETURNS)
    prices = await service.create(name="Prices")
    await service.add_manual_source(prices.id, title="Coverage", body=COVERAGE)
    await service.attach(policies.id, agent.id)
    await service.attach(prices.id, agent.id)

    result = await service.retrieve("coverage and returns", agent_id=agent.id)

    assert {passage.citation.kb_id for passage in result.passages} == {policies.id, prices.id}


async def test_an_agent_with_nothing_attached_reports_no_context(
    session: AsyncSession, tenant: Tenant
) -> None:
    service = KnowledgeBaseService(session, tenant.id)
    agent = await AgentService(session, tenant.id).create(name="Bare Agent")

    result = await service.retrieve("anything", agent_id=agent.id)

    assert not result.has_context
    assert result.no_context_reason is NoContextReason.EMPTY_KNOWLEDGE_BASE


async def test_one_keyword_knowledge_base_makes_the_whole_agent_retrieval_keyword(
    session: AsyncSession, tenant: Tenant, config_override: Callable[..., None]
) -> None:
    """Injecting one knowledge base whole beside a search result from another is a muddle."""
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=100_000)
    service = KnowledgeBaseService(session, tenant.id)
    agent = await AgentService(session, tenant.id).create(name="Sales Assistant")
    small = await service.create(name="Small", retrieval_tier=RetrievalTier.DIRECT)
    await service.add_manual_source(small.id, title="Coverage", body=COVERAGE)
    pinned = await service.create(name="Pinned", retrieval_tier=RetrievalTier.KEYWORD)
    await service.add_manual_source(pinned.id, title="Returns", body=RETURNS)
    await service.attach(small.id, agent.id)
    await service.attach(pinned.id, agent.id)

    result = await service.retrieve("tinted paint", agent_id=agent.id)

    assert result.tier is RetrievalTier.KEYWORD


async def test_retrieval_requires_exactly_one_scope(service: KnowledgeBaseService) -> None:
    with pytest.raises(ValidationException):
        await service.retrieve("anything")
    with pytest.raises(ValidationException):
        await service.retrieve("anything", kb_id=uuid.uuid4(), agent_id=uuid.uuid4())


# -- isolation ----------------------------------------------------------------------------


async def test_retrieval_cannot_reach_another_tenants_knowledge(
    session: AsyncSession,
    make_tenant: Callable[..., Coroutine[Any, Any, Tenant]],
    config_override: Callable[..., None],
) -> None:
    """Invariant 2, at the one place a leak would be least visible — the text is never returned
    to a caller directly, it goes straight into someone's prompt."""
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=100_000)
    first = await make_tenant(name="Tenant A")
    second = await make_tenant(name="Tenant B")

    theirs = KnowledgeBaseService(session, first.id)
    secret = await theirs.create(name="Confidential")
    await theirs.add_manual_source(secret.id, title="Margins", body="Our margin is 42 percent.")

    mine = KnowledgeBaseService(session, second.id)
    with pytest.raises(Exception) as caught:
        await mine.retrieve("margin", kb_id=secret.id)

    assert "KB_NOT_FOUND" in str(getattr(caught.value, "code", ""))
