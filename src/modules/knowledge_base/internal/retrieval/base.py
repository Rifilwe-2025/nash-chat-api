"""What a retrieval returns, whichever tier produced it (spec §5.2.2).

One result shape for both tiers is the point of the whole package: Phase 7 assembles a prompt from
a :class:`RetrievalResult` without knowing or caring whether the text was injected whole or pulled
out by a search. When Tier 3 (vectors) arrives in v2 it fills in the same object.

Two fields carry more weight than their size suggests:

``has_context`` is the explicit "no relevant context found" signal. An empty passage list and a
knowledge base that genuinely had no answer must be distinguishable from a failure, because the
agent's behaviour differs: with no context it uses its configured fallback response rather than
guessing (spec §5.2, §5.1).

``passages[].citation`` is the source metadata every retrieval carries, for the logging and
debugging §5.2 asks for — which document an answer came from is the first question anyone asks
about a wrong answer.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field

from src.modules.knowledge_base.domain.models import RetrievalTier


class NoContextReason(str, enum.Enum):
    """Why a retrieval came back empty. Never conflated with an error."""

    EMPTY_KNOWLEDGE_BASE = "empty_knowledge_base"
    NO_MATCH = "no_match"
    BELOW_THRESHOLD = "below_threshold"


@dataclass(frozen=True, slots=True)
class Citation:
    """Where a passage came from (spec §5.2: source citation tracking)."""

    source_id: uuid.UUID
    kb_id: uuid.UUID
    source_name: str
    source_type: str
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Passage:
    """A piece of knowledge with its provenance.

    In Tier 1 the passage is a whole source. In Tier 2 it is the fragment Postgres cut out around
    the match — computed at query time, not a stored chunk.
    """

    text: str
    citation: Citation
    score: float | None = None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    tier: RetrievalTier
    passages: list[Passage] = field(default_factory=list)
    no_context_reason: NoContextReason | None = None
    # What the router saw when it chose. Kept for the explain endpoint and for logs — a retrieval
    # that picked the wrong tier is otherwise very hard to diagnose after the fact.
    considered_characters: int = 0
    budget_characters: int = 0

    @property
    def has_context(self) -> bool:
        return bool(self.passages)

    @property
    def characters(self) -> int:
        return sum(len(passage.text) for passage in self.passages)

    @property
    def citations(self) -> list[Citation]:
        return [passage.citation for passage in self.passages]
