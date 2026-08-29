"""Tier selection (spec §5.2.2).

The decision itself is a pure function, so it is tested as one — including at the size boundary,
which is the case the phase's "done when" calls out and the one most likely to be got wrong by an
off-by-one.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from src.modules.knowledge_base.domain.models import RetrievalTier
from src.modules.knowledge_base.internal.retrieval import choose_tier, injection_budget
from src.shared.llm.context import DEFAULT_CONTEXT_TOKENS, context_characters, context_tokens

# -- the budget --------------------------------------------------------------------


def test_the_budget_is_a_fraction_of_the_window_not_the_whole_window(
    config_override: Callable[..., None],
) -> None:
    """History and the system prompt share that window, and every injected token is paid for."""
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=10_000_000, KB_CONTEXT_BUDGET_FRACTION=0.25)

    assert injection_budget("gpt-4o") == int(context_characters("gpt-4o") * 0.25)


def test_a_huge_context_window_is_still_capped(config_override: Callable[..., None]) -> None:
    """A million-token window is not a reason to spend a million tokens on knowledge each turn."""
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=48_000, KB_CONTEXT_BUDGET_FRACTION=0.25)

    assert injection_budget("gemini-2.0-flash") == 48_000


def test_an_unknown_model_gets_the_conservative_default() -> None:
    assert context_tokens("some-model-shipped-next-year") == DEFAULT_CONTEXT_TOKENS
    assert context_tokens(None) == DEFAULT_CONTEXT_TOKENS


def test_the_longest_matching_prefix_wins() -> None:
    assert context_tokens("gemini-2.0-flash") == context_tokens("gemini-2.5-pro")
    assert context_tokens("claude-opus-5") > 0


# -- automatic selection ------------------------------------------------------------


def test_content_within_the_budget_is_injected_whole(
    config_override: Callable[..., None],
) -> None:
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=1_000, KB_CONTEXT_BUDGET_FRACTION=1.0)

    decision = choose_tier(RetrievalTier.AUTO, total_characters=500, model="gpt-4o")

    assert decision.tier is RetrievalTier.DIRECT
    assert decision.forced is False
    assert "fit within" in decision.reason


def test_content_over_the_budget_is_searched_instead(
    config_override: Callable[..., None],
) -> None:
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=1_000, KB_CONTEXT_BUDGET_FRACTION=1.0)

    decision = choose_tier(RetrievalTier.AUTO, total_characters=1_001, model="gpt-4o")

    assert decision.tier is RetrievalTier.KEYWORD
    assert "exceed" in decision.reason


def test_content_exactly_on_the_boundary_is_still_injected(
    config_override: Callable[..., None],
) -> None:
    """The boundary case the phase calls out: the budget is inclusive."""
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=1_000, KB_CONTEXT_BUDGET_FRACTION=1.0)

    assert choose_tier(RetrievalTier.AUTO, 1_000, model="gpt-4o").tier is RetrievalTier.DIRECT
    assert choose_tier(RetrievalTier.AUTO, 1_001, model="gpt-4o").tier is RetrievalTier.KEYWORD


def test_the_same_knowledge_base_can_route_differently_per_model(
    config_override: Callable[..., None],
) -> None:
    """The budget is the model's, so a bigger window moves the boundary — that is the point."""
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=10_000_000, KB_CONTEXT_BUDGET_FRACTION=0.25)
    size = context_characters("gpt-4o") // 2  # over gpt-4o's quarter, under Gemini's

    assert choose_tier(RetrievalTier.AUTO, size, model="gpt-4o").tier is RetrievalTier.KEYWORD
    assert (
        choose_tier(RetrievalTier.AUTO, size, model="gemini-2.0-flash").tier is RetrievalTier.DIRECT
    )


# -- manual override -----------------------------------------------------------------


@pytest.mark.parametrize("pinned", [RetrievalTier.DIRECT, RetrievalTier.KEYWORD])
def test_a_pinned_tier_is_honoured_whatever_the_size(
    pinned: RetrievalTier, config_override: Callable[..., None]
) -> None:
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=1_000, KB_CONTEXT_BUDGET_FRACTION=1.0)

    for size in (10, 10_000_000):
        decision = choose_tier(pinned, total_characters=size, model="gpt-4o")
        assert decision.tier is pinned
        assert decision.forced is True
        assert "pinned" in decision.reason


def test_the_decision_records_what_it_saw(config_override: Callable[..., None]) -> None:
    """Without these numbers a retrieval that picked the wrong tier is undiagnosable later."""
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=1_000, KB_CONTEXT_BUDGET_FRACTION=1.0)

    decision = choose_tier(RetrievalTier.AUTO, total_characters=750, model="gpt-4o")

    assert decision.considered_characters == 750
    assert decision.budget_characters == 1_000
