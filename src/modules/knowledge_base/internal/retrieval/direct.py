"""Tier 1 — direct injection (spec §5.2.2).

The whole extracted text of every ready source, assembled with its source metadata, for the prompt
to carry verbatim. No query is involved: Tier 1 does not select, it hands over everything, because
for a short FAQ or a handful of policy documents the model reading all of it is both simpler and
better than any retrieval heuristic.

Sources are kept whole and labelled. The label is not decoration — Phase 7 delimits injected
knowledge from instructions (§5.7, invariant 3), and a passage that arrives already attributed to a
named document is what makes that delimiting possible and what makes a wrong answer traceable.
"""

from __future__ import annotations

from collections.abc import Sequence

from src.modules.knowledge_base.domain.models import KbSource, RetrievalTier
from src.modules.knowledge_base.internal.retrieval.base import (
    Citation,
    NoContextReason,
    Passage,
    RetrievalResult,
)


def citation_for(source: KbSource) -> Citation:
    url = source.config_json.get("url") if isinstance(source.config_json, dict) else None
    return Citation(
        source_id=source.id,
        kb_id=source.kb_id,
        source_name=source.name,
        source_type=source.type.value,
        url=url if isinstance(url, str) else None,
    )


def retrieve_direct(
    sources: Sequence[KbSource],
    considered_characters: int = 0,
    budget_characters: int = 0,
) -> RetrievalResult:
    """Assemble every ready source in full.

    A source that failed extraction, or is still being processed, simply is not here: it has no
    text to inject, and injecting an error message as though it were knowledge would be worse than
    omitting it.
    """
    passages = [
        Passage(text=source.extracted_text.strip(), citation=citation_for(source))
        for source in sources
        if source.extracted_text and source.extracted_text.strip()
    ]

    return RetrievalResult(
        tier=RetrievalTier.DIRECT,
        passages=passages,
        no_context_reason=None if passages else NoContextReason.EMPTY_KNOWLEDGE_BASE,
        considered_characters=considered_characters,
        budget_characters=budget_characters,
    )
