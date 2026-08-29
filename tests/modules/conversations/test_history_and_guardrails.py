"""History trimming, cost recording, and guardrail decisions — the pure logic (spec §5.4).

All three are decided without a database or a provider, so they are tested that way: these are the
rules, and the service test that follows checks they are wired up.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from src.modules.conversations.internal import guardrails
from src.modules.conversations.internal.history.trimming import (
    HistoryTurn,
    history_budget,
    trim,
)
from src.shared.llm.pricing import cost_micro_usd, price_for


def turn(role: str, size: int) -> HistoryTurn:
    return HistoryTurn(role=role, content="x" * size)


def exchange(size: int) -> list[HistoryTurn]:
    return [turn("user", size), turn("assistant", size)]


# -- the budget ---------------------------------------------------------------------


def test_room_is_reserved_for_the_model_to_write_into() -> None:
    """Leaving output room out is how a request that fits on paper still truncates mid-sentence."""
    without_output = history_budget(
        context_characters=10_000,
        system_prompt_characters=1_000,
        max_output_tokens=0,
        reserve_fraction=1.0,
    )
    with_output = history_budget(
        context_characters=10_000,
        system_prompt_characters=1_000,
        max_output_tokens=500,
        reserve_fraction=1.0,
    )

    assert with_output < without_output


def test_a_system_prompt_that_fills_the_window_leaves_no_history() -> None:
    """Knowledge and guardrails are not negotiable; history is what yields."""
    budget = history_budget(
        context_characters=1_000,
        system_prompt_characters=5_000,
        max_output_tokens=100,
        reserve_fraction=0.5,
    )

    assert budget == 0


# -- trimming -----------------------------------------------------------------------


def test_everything_is_kept_when_it_fits() -> None:
    turns = [*exchange(10), *exchange(10)]

    result = trim(turns, budget_characters=1_000)

    assert result.kept == turns
    assert result.dropped == []


def test_the_oldest_turns_are_dropped_first() -> None:
    """The last few turns carry the thread; the first few rarely do."""
    turns = [turn("user", 10), turn("assistant", 10), turn("user", 20), turn("assistant", 20)]

    result = trim(turns, budget_characters=40)

    assert result.kept == turns[2:]
    assert result.dropped == turns[:2]


def test_a_reply_is_never_kept_without_its_question() -> None:
    """An assistant turn on its own reads as the model volunteering something strange."""
    turns = [turn("user", 100), turn("assistant", 30)]

    result = trim(turns, budget_characters=50)

    assert result.kept == [], "the pair does not fit, so neither half is kept"
    assert result.dropped == turns


def test_a_single_turn_larger_than_the_budget_keeps_nothing() -> None:
    """Half a message is worse than none — the summary already covers what was said."""
    result = trim([turn("user", 500)], budget_characters=100)

    assert result.kept == []
    assert len(result.dropped) == 1


def test_an_empty_history_is_handled() -> None:
    result = trim([], budget_characters=100)

    assert result.kept == []
    assert result.dropped == []


def test_a_long_conversation_stays_inside_the_budget() -> None:
    """The phase's bar: however long someone talks, the prompt stays bounded."""
    turns = [item for _ in range(200) for item in exchange(50)]

    result = trim(turns, budget_characters=1_000)

    assert result.kept_characters <= 1_000
    assert len(result.dropped) > 0


# -- cost recording -------------------------------------------------------------------


def test_no_cost_is_recorded_for_a_model_with_no_configured_price(
    config_override: Callable[..., None],
) -> None:
    """The platform records tokens it measured and never invents a price it was not given."""
    config_override(LLM_PRICE_TABLE="")

    assert price_for("gpt-4o") is None
    assert cost_micro_usd("gpt-4o", 1_000, 1_000) is None


def test_cost_is_computed_from_the_configured_price(
    config_override: Callable[..., None],
) -> None:
    config_override(LLM_PRICE_TABLE="gpt-4o=2.5/10")

    # 1M input at $2.50 plus 1M output at $10.00 = $12.50 = 12_500_000 micro-USD.
    assert cost_micro_usd("gpt-4o", 1_000_000, 1_000_000) == 12_500_000
    assert cost_micro_usd("gpt-4o", 1_000, 0) == 2_500


def test_the_longest_matching_prefix_wins(config_override: Callable[..., None]) -> None:
    """A family default and a specific model can coexist."""
    config_override(LLM_PRICE_TABLE="claude=3/15,claude-opus=15/75")

    price = price_for("claude-opus-5")
    assert price is not None
    assert price.input_per_million == 15


def test_a_malformed_price_entry_is_ignored_rather_than_crashing(
    config_override: Callable[..., None],
) -> None:
    config_override(LLM_PRICE_TABLE="nonsense,gpt-4o=2.5/10,broken=abc/def")

    assert price_for("gpt-4o") is not None
    assert price_for("broken") is None


# -- guardrails -------------------------------------------------------------------------


def test_an_escalation_trigger_hands_the_conversation_over() -> None:
    decision = guardrails.evaluate(
        "I would like to speak to a manager please",
        escalation_triggers=["speak to a manager"],
        restricted_topics=[],
    )

    assert decision.action is guardrails.GuardrailAction.ESCALATE
    assert decision.matched == "speak to a manager"


def test_a_restricted_topic_is_declined() -> None:
    decision = guardrails.evaluate(
        "Can you give me legal advice about this?",
        escalation_triggers=[],
        restricted_topics=["legal advice"],
    )

    assert decision.action is guardrails.GuardrailAction.DECLINE
    assert decision.blocks_model_call


def test_escalation_wins_over_a_restricted_topic() -> None:
    """Someone asking for a human and a restricted topic should get the human, not a refusal."""
    decision = guardrails.evaluate(
        "I need legal advice, put me through to a manager",
        escalation_triggers=["manager"],
        restricted_topics=["legal advice"],
    )

    assert decision.action is guardrails.GuardrailAction.ESCALATE


def test_matching_is_on_whole_words_not_substrings() -> None:
    """ "cancel" must fire on "I want to cancel" and not on "cancellation policy"."""
    triggers = ["cancel"]

    assert (
        guardrails.evaluate("I want to cancel", triggers, []).action
        is guardrails.GuardrailAction.ESCALATE
    )
    assert (
        guardrails.evaluate("What is your cancellation policy?", triggers, []).action
        is guardrails.GuardrailAction.ALLOW
    )


def test_matching_ignores_case_and_extra_spacing() -> None:
    decision = guardrails.evaluate("SPEAK  TO   A MANAGER", ["speak to a manager"], [])

    assert decision.action is guardrails.GuardrailAction.ESCALATE


@pytest.mark.parametrize("configured", [[], [""], ["   "]])
def test_no_configured_rules_means_everything_is_allowed(configured: list[str]) -> None:
    decision = guardrails.evaluate("anything at all", configured, configured)

    assert decision.action is guardrails.GuardrailAction.ALLOW


def test_the_escalation_notice_is_not_the_knowledge_base_fallback() -> None:
    """Being handed to a human is a different thing to be told than "I don't know that"."""
    fallback = "I don't have that information."

    assert guardrails.escalation_response() != fallback
    assert guardrails.decline_response(fallback) == fallback
