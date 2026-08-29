"""Tiered retrieval (spec §5.2.2).

Module-private, like everything under ``internal/``. Callers — including Phase 7's prompt
assembly — go through ``KnowledgeBaseService.retrieve``, which is one method whatever tier runs, so
nothing outside this package ever branches on the tier itself.
"""

from src.modules.knowledge_base.internal.retrieval.base import (
    Citation,
    NoContextReason,
    Passage,
    RetrievalResult,
)
from src.modules.knowledge_base.internal.retrieval.direct import citation_for, retrieve_direct
from src.modules.knowledge_base.internal.retrieval.keyword import retrieve_keyword
from src.modules.knowledge_base.internal.retrieval.router import (
    TierDecision,
    choose_tier,
    injection_budget,
)

__all__ = [
    "Citation",
    "NoContextReason",
    "Passage",
    "RetrievalResult",
    "TierDecision",
    "choose_tier",
    "citation_for",
    "injection_budget",
    "retrieve_direct",
    "retrieve_keyword",
]
