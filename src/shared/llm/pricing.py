"""Per-model pricing, supplied by configuration rather than hardcoded.

Cost per message is a deliverable (§5.8), but prices change, differ per account, and differ again
for tenants who bring their own keys (§9). A table baked into the source would be wrong somewhere
almost immediately, and a *confidently wrong* cost figure is worse than none — so nothing is assumed
here. A model with no configured price records no cost, and the tokens, which are measured, are
stored either way.

Configure with ``LLM_PRICE_TABLE`` as a comma-separated list of
``<model-prefix>=<input>/<output>``, in USD per million tokens::

    LLM_PRICE_TABLE=gpt-4o=2.5/10,claude-sonnet-4-5=3/15

The longest matching prefix wins, so a family default and a specific model can coexist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src import configs

logger = logging.getLogger("api.llm.pricing")

MICROS_PER_USD = 1_000_000
TOKENS_PER_PRICE_UNIT = 1_000_000


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens, in and out."""

    input_per_million: float
    output_per_million: float


def _parse(entries: list[str]) -> dict[str, ModelPrice]:
    table: dict[str, ModelPrice] = {}
    for entry in entries:
        model, separator, prices = entry.partition("=")
        if not separator or "/" not in prices:
            logger.warning("ignoring malformed LLM_PRICE_TABLE entry %r", entry)
            continue
        raw_input, _, raw_output = prices.partition("/")
        try:
            table[model.strip().lower()] = ModelPrice(float(raw_input), float(raw_output))
        except ValueError:
            logger.warning("ignoring LLM_PRICE_TABLE entry with non-numeric price %r", entry)
    return table


def price_table() -> dict[str, ModelPrice]:
    """Read afresh each call so a configuration reload takes effect without a restart."""
    return _parse(list(configs.LLM_PRICE_TABLE))


def price_for(model: str | None) -> ModelPrice | None:
    if not model:
        return None
    table = price_table()
    normalised = model.strip().lower()
    matches = [prefix for prefix in table if normalised.startswith(prefix)]
    if not matches:
        return None
    return table[max(matches, key=len)]


def cost_micro_usd(model: str | None, prompt_tokens: int, completion_tokens: int) -> int | None:
    """Cost of one call in millionths of a dollar, or ``None`` when the model has no price set."""
    price = price_for(model)
    if price is None:
        return None

    dollars = (
        prompt_tokens * price.input_per_million + completion_tokens * price.output_per_million
    ) / TOKENS_PER_PRICE_UNIT
    return round(dollars * MICROS_PER_USD)
