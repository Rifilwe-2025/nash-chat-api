"""Tier 2 — keyword search (spec §5.2.2).

For a knowledge base too large to inject every turn, Postgres full-text search picks the relevant
passages first. No embedding model, no vector index, no chunk table: the ranking and the passage
extraction both happen inside the query, against text that is already stored.

The SQL itself lives in ``domain/repositories.py``, where every ``select(...)`` in this module
belongs. What lives here is the judgement applied to its results — chiefly the relevance threshold,
which is the difference between "here is the answer" and "here are three paragraphs that share a
common word with the question". Below the threshold this reports **no context** rather than
injecting noise, because an agent told nothing relevant was found gives its fallback response,
while an agent handed irrelevant text will answer from it (spec §5.2, §5.1).
"""

from __future__ import annotations

from collections.abc import Sequence

from src.modules.knowledge_base.domain.models import KbSource, RetrievalTier
from src.modules.knowledge_base.internal.retrieval.base import (
    NoContextReason,
    Passage,
    RetrievalResult,
)
from src.modules.knowledge_base.internal.retrieval.direct import citation_for


def retrieve_keyword(
    matches: Sequence[tuple[KbSource, float, str]],
    min_rank: float,
    considered_characters: int = 0,
    budget_characters: int = 0,
) -> RetrievalResult:
    """Turn ranked search rows into passages, dropping anything below the threshold.

    ``matches`` arrives ordered by rank, so the first row is the best available answer; if even
    that is below the threshold the whole result is noise and none of it is worth injecting.
    """
    if not matches:
        return _empty(NoContextReason.NO_MATCH, considered_characters, budget_characters)

    passages = [
        Passage(text=headline.strip(), citation=citation_for(source), score=rank)
        for source, rank, headline in matches
        if rank >= min_rank and headline.strip()
    ]

    if not passages:
        return _empty(NoContextReason.BELOW_THRESHOLD, considered_characters, budget_characters)

    return RetrievalResult(
        tier=RetrievalTier.KEYWORD,
        passages=passages,
        considered_characters=considered_characters,
        budget_characters=budget_characters,
    )


def _empty(
    reason: NoContextReason, considered_characters: int, budget_characters: int
) -> RetrievalResult:
    return RetrievalResult(
        tier=RetrievalTier.KEYWORD,
        passages=[],
        no_context_reason=reason,
        considered_characters=considered_characters,
        budget_characters=budget_characters,
    )
