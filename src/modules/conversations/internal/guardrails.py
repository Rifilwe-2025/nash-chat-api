"""Guardrail decisions, made in code (spec §5.1, §5.4).

Escalation and restricted topics are decided here, deterministically, against the raw user message —
**not** delegated to the model. Two reasons, and the second is the important one:

* A tenant who writes "escalate when someone asks for a refund" expects that to happen every time,
  not usually.
* The model is the component under attack (§5.7). A conversation that can talk the model out of
  escalating is a conversation that can talk it out of handing off to a human, which is precisely
  the case where a human is most needed.

Matching is word-boundary aware rather than substring: a trigger of "cancel" should fire on "I want
to cancel" and not on "cancellation policy", and certainly not inside an unrelated word. It is a
blunt instrument, deliberately — an agent that escalates slightly too eagerly is a nuisance, while
one that fails to escalate is the failure mode the tenant configured it to avoid.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class GuardrailAction(str, enum.Enum):
    ALLOW = "allow"
    ESCALATE = "escalate"
    DECLINE = "decline"


@dataclass(frozen=True, slots=True)
class GuardrailDecision:
    action: GuardrailAction
    matched: str | None = None
    reason: str | None = None

    @property
    def blocks_model_call(self) -> bool:
        """A declined topic is answered without ever reaching the provider."""
        return self.action is GuardrailAction.DECLINE


def _matches(phrase: str, text: str) -> bool:
    """True when ``phrase`` appears in ``text`` as whole words."""
    cleaned = phrase.strip()
    if not cleaned:
        return False
    pattern = r"\b" + r"\W+".join(re.escape(word) for word in cleaned.split()) + r"\b"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def first_match(phrases: list[str], text: str) -> str | None:
    return next((phrase for phrase in phrases if _matches(phrase, text)), None)


def evaluate(
    message: str,
    escalation_triggers: list[str],
    restricted_topics: list[str],
) -> GuardrailDecision:
    """Decide what to do with an incoming message before the model sees it.

    Escalation is checked first: a customer asking about a restricted topic *and* demanding a human
    should get the human. Declining would leave them stuck with an agent that will not help.
    """
    triggered = first_match(escalation_triggers, message)
    if triggered is not None:
        return GuardrailDecision(
            action=GuardrailAction.ESCALATE,
            matched=triggered,
            reason=f"Message matched the escalation trigger {triggered!r}.",
        )

    restricted = first_match(restricted_topics, message)
    if restricted is not None:
        return GuardrailDecision(
            action=GuardrailAction.DECLINE,
            matched=restricted,
            reason=f"Message matched the restricted topic {restricted!r}.",
        )

    return GuardrailDecision(action=GuardrailAction.ALLOW)


DEFAULT_DECLINE = "I'm not able to help with that one, but I'm happy to help with anything else."
DEFAULT_ESCALATION = "Let me put you through to a member of the team who can help with this."


def decline_response(fallback: str | None) -> str:
    return fallback.strip() if fallback and fallback.strip() else DEFAULT_DECLINE


def escalation_response() -> str:
    """The escalation notice is its own message, never the knowledge-base fallback.

    A tenant's fallback is written for "I don't know that". Being handed to a human is a different
    thing to be told, and borrowing that sentence here reads as the agent giving up rather than as
    help arriving. A tenant-configurable handoff message belongs with the handoff transport in
    Phase 10, where there is somewhere for the conversation to actually go.
    """
    return DEFAULT_ESCALATION
