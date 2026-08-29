"""Which tier answers a query (spec §5.2.2).

The decision is a size comparison and nothing more: if everything the knowledge bases hold fits in
the budget, inject it (Tier 1); otherwise search it (Tier 2). Automatic by default, because the
right tier is a property of how much content a tenant has uploaded, not something they should have
to notice and reconfigure as it grows.

**The budget is not the model's context window.** It is the smaller of a flat cap and a fraction of
that window, for two reasons: history and the system prompt need room in the same window, and a
model that *can* hold a million tokens still charges for every one of them on every turn. A large
window makes Tier 1 possible for a bigger knowledge base; it does not make injecting everything a
good idea.

Tier 3 (vectors) is v2 (§5.2.4). When it arrives it becomes another branch here — which is the
whole reason the choice is one function rather than a condition spread across callers.
"""

from __future__ import annotations

from dataclasses import dataclass

from src import configs
from src.modules.knowledge_base.domain.models import RetrievalTier
from src.shared.llm.context import context_characters


@dataclass(frozen=True, slots=True)
class TierDecision:
    tier: RetrievalTier
    budget_characters: int
    considered_characters: int
    forced: bool

    @property
    def reason(self) -> str:
        """Plain-language explanation, for the explain endpoint and for logs."""
        if self.forced:
            return f"the knowledge base is pinned to the {self.tier.value} tier"
        if self.tier is RetrievalTier.DIRECT:
            return (
                f"{self.considered_characters} characters of knowledge fit within the "
                f"{self.budget_characters} character budget, so all of it is injected"
            )
        return (
            f"{self.considered_characters} characters of knowledge exceed the "
            f"{self.budget_characters} character budget, so the query is searched instead"
        )


def injection_budget(model: str | None) -> int:
    """How much knowledge may be injected whole for ``model``."""
    flat_cap: int = configs.KNOWLEDGE_BASE_DIRECT_INJECTION_MAX_CHARS
    fraction: float = configs.KNOWLEDGE_BASE_CONTEXT_BUDGET_FRACTION
    return min(flat_cap, int(context_characters(model) * fraction))


def choose_tier(
    configured: RetrievalTier,
    total_characters: int,
    model: str | None = None,
) -> TierDecision:
    """Pick the tier for one retrieval.

    ``configured`` is the knowledge base's own setting: ``AUTO`` defers to the size comparison,
    anything else is a manual override and is honoured as given — including a small knowledge base
    pinned to ``KEYWORD``, which is a reasonable thing to want when only the relevant paragraph
    should reach the prompt.
    """
    budget = injection_budget(model)

    if configured is not RetrievalTier.AUTO:
        return TierDecision(
            tier=configured,
            budget_characters=budget,
            considered_characters=total_characters,
            forced=True,
        )

    tier = RetrievalTier.DIRECT if total_characters <= budget else RetrievalTier.KEYWORD
    return TierDecision(
        tier=tier,
        budget_characters=budget,
        considered_characters=total_characters,
        forced=False,
    )
