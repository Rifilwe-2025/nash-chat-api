"""Fitting a conversation into the model's budget (spec §5.4).

Two rules, both of which matter more than they look:

**Keep the most recent turns.** History is trimmed from the *oldest* end, because the last few turns
carry the thread of what is being discussed and the first few rarely do. What is dropped is not
lost — it is folded into the rolling summary first (see ``summarisation.py``).

**Never split a pair.** An assistant reply without the question that prompted it reads as the model
volunteering something strange, and models do notice. So trimming works in exchanges: if a user
turn does not fit, its reply goes too.

The budget is a share of what is left after the system prompt, which already contains the persona,
the guardrails and the injected knowledge — all of which the turn cannot do without. History is what
yields when there is not enough room.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.shared.llm.context import CHARACTERS_PER_TOKEN


@dataclass(frozen=True, slots=True)
class HistoryTurn:
    """One stored turn, in the terms trimming cares about."""

    role: str
    content: str

    @property
    def characters(self) -> int:
        return len(self.content)


@dataclass(frozen=True, slots=True)
class TrimResult:
    kept: list[HistoryTurn]
    dropped: list[HistoryTurn]
    budget_characters: int

    @property
    def kept_characters(self) -> int:
        return sum(turn.characters for turn in self.kept)


def history_budget(
    context_characters: int,
    system_prompt_characters: int,
    max_output_tokens: int,
    reserve_fraction: float,
) -> int:
    """What is left for history once everything non-negotiable has its share.

    The output reservation is real room the model needs to *write* into; leaving it out is how a
    request that fits on paper still fails, or truncates mid-sentence.
    """
    reserved_for_output = max_output_tokens * CHARACTERS_PER_TOKEN
    available = context_characters - system_prompt_characters - reserved_for_output
    return max(int(available * reserve_fraction), 0)


def trim(turns: Sequence[HistoryTurn], budget_characters: int) -> TrimResult:
    """Keep as many recent exchanges as fit, oldest dropped first.

    Walks backwards accumulating whole exchanges. A single turn larger than the entire budget
    means nothing fits, and the result is empty rather than truncated: half a message is worse
    than none, and the summary already covers what was said.
    """
    kept: list[HistoryTurn] = []
    used = 0

    index = len(turns) - 1
    while index >= 0:
        # An assistant reply is taken together with the user turn immediately before it.
        if turns[index].role == "assistant" and index > 0 and turns[index - 1].role == "user":
            exchange = [turns[index - 1], turns[index]]
            step = 2
        else:
            exchange = [turns[index]]
            step = 1

        size = sum(turn.characters for turn in exchange)
        if used + size > budget_characters:
            break

        kept = [*exchange, *kept]
        used += size
        index -= step

    dropped = list(turns[: len(turns) - len(kept)])
    return TrimResult(kept=kept, dropped=dropped, budget_characters=budget_characters)
