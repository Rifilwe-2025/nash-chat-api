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

import uuid
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
    relative_floor: float = 0.0,
    considered_characters: int = 0,
    budget_characters: int = 0,
) -> RetrievalResult:
    """Turn ranked search rows into passages, dropping anything not worth injecting.

    Two thresholds, because relevance is comparative and ``ts_rank_cd`` is not stable across
    queries. Its scores fall as a question grows, so one absolute floor is at once too high for a
    sentence and too low for two words: "Chitungwiza branch address" scored 0.028 while "I stay in
    Chitungwiza, is there a Nash Paints branch near me? I need the address and phone number"
    scored under 0.005 against the same document. The floor turned the second into "there is no
    branch in Chitungwiza" — about a town that has one.

    So ``min_rank`` is only the cheap "did anything match at all" gate, and ``relative_floor``
    does the real filtering, against the best row actually found. A weak best match keeps the rows
    beside it rather than none; a strong one still drops the tail that merely shares a common word.

    Compared **within** each knowledge base, never across them. Scores are not comparable between
    bodies of knowledge any more than they are between queries: a library of long datasheets
    outscores a file of short price lines on the same question, and one floor taken from the best
    row overall would discard the smaller knowledge base entirely — the starvation the repository
    layer already shares slots out to prevent.
    """
    if not matches:
        return _empty(NoContextReason.NO_MATCH, considered_characters, budget_characters)

    best_per_kb: dict[uuid.UUID, float] = {}
    for source, rank, _ in matches:
        if rank > best_per_kb.get(source.kb_id, 0.0):
            best_per_kb[source.kb_id] = rank

    passages = [
        Passage(text=headline.strip(), citation=citation_for(source), score=rank)
        for source, rank, headline in matches
        if rank >= max(min_rank, best_per_kb[source.kb_id] * relative_floor) and headline.strip()
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
