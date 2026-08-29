"""How much room a model has, for callers that must decide what to send it.

Used by knowledge base tier routing (spec §5.2.2): whether a knowledge base can be injected whole
depends on what the agent's model can actually hold.

**These are deliberately conservative floors, not exact specifications.** Context windows change
with every model release, and this table is not the place to track them. Erring low is the safe
direction: underestimating means a knowledge base is searched rather than injected, which still
answers the question — overestimating means a request the provider rejects. An unrecognised model
gets :data:`DEFAULT_CONTEXT_TOKENS`, which every current model comfortably exceeds.

The characters-per-token ratio is likewise a rule of thumb. Callers should spend a *fraction* of
what this returns rather than filling it, since history and the system prompt need room too.
"""

from __future__ import annotations

# Conservative floors by model-id prefix, longest prefix winning.
CONTEXT_TOKENS: dict[str, int] = {
    "claude-": 200_000,
    "gpt-4o": 128_000,
    "gpt-4.1": 128_000,
    "gpt-5": 128_000,
    "o1": 128_000,
    "o3": 128_000,
    "gemini-1.5": 1_000_000,
    "gemini-2": 1_000_000,
    "gemini-3": 1_000_000,
}

DEFAULT_CONTEXT_TOKENS = 128_000

# English prose averages nearer 4 characters per token; 4 is the usual working figure.
CHARACTERS_PER_TOKEN = 4


def context_tokens(model: str | None) -> int:
    """A safe lower bound on the context window of ``model``."""
    if not model:
        return DEFAULT_CONTEXT_TOKENS

    normalised = model.strip().lower()
    matches = [prefix for prefix in CONTEXT_TOKENS if normalised.startswith(prefix)]
    if not matches:
        return DEFAULT_CONTEXT_TOKENS
    return CONTEXT_TOKENS[max(matches, key=len)]


def context_characters(model: str | None) -> int:
    """The same bound expressed in characters, which is what text budgets are measured in."""
    return context_tokens(model) * CHARACTERS_PER_TOKEN
