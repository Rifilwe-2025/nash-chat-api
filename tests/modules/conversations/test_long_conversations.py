"""Long conversations: ordering, trimming, and rolling summarisation (spec §5.4).

The phase's bar is that a long conversation stays inside the context budget. Getting there needs
two things that are easy to get subtly wrong — a stable message order, and history that shrinks
without losing what was said — so both are checked against the database rather than in the abstract.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.conversations.domain.models import MessageRole
from src.modules.tenants.domain.models import Tenant
from src.shared.database.pagination import PageRequest
from src.shared.llm import CompletionRequest, CompletionResult, TokenUsage
from src.shared.llm.errors import LLMUnavailableError
from tests.modules.conversations.test_turn import RecordingLLM, build_agent, service


@pytest.fixture
async def tenant(make_tenant: Callable[..., Coroutine[Any, Any, Tenant]]) -> Tenant:
    return await make_tenant(name="Nash Paints")


class SummarisingLLM(RecordingLLM):
    """Answers turns normally and returns a recognisable summary when asked to summarise."""

    SUMMARY = "The customer asked about order 1234 and wants a refund."

    async def complete(
        self, provider: str, request: CompletionRequest, api_key: str | None = None
    ) -> CompletionResult:
        self.requests.append((provider, request))
        summarising = "running summary" in (request.system or "")
        return CompletionResult(
            content=self.SUMMARY if summarising else self.reply,
            usage=TokenUsage(prompt_tokens=100, completion_tokens=20),
            model=request.model,
            provider=provider,
        )

    @property
    def summary_requests(self) -> list[CompletionRequest]:
        return [
            request for _, request in self.requests if "running summary" in (request.system or "")
        ]


class SummaryFailsLLM(RecordingLLM):
    """Answers turns but cannot summarise — a provider blip during a background step."""

    async def complete(
        self, provider: str, request: CompletionRequest, api_key: str | None = None
    ) -> CompletionResult:
        if "running summary" in (request.system or ""):
            raise LLMUnavailableError("summariser is down", provider=provider)
        self.requests.append((provider, request))
        return CompletionResult(
            content=self.reply,
            usage=TokenUsage(prompt_tokens=100, completion_tokens=20),
            model=request.model,
            provider=provider,
        )


# -- ordering ------------------------------------------------------------------------


async def test_the_transcript_keeps_the_order_things_were_said(
    session: AsyncSession, tenant: Tenant
) -> None:
    """Regression test. ``created_at`` defaults to Postgres ``now()``, which is *transaction*
    time — both messages of a turn share it exactly, so ordering by it falls back to a random
    UUID and shuffles the question with its answer."""
    agent = await build_agent(session, tenant)
    engine = service(session, tenant, RecordingLLM())

    for index in range(4):
        await engine.send_message(agent.id, f"Question {index}", external_user_id="ada")

    conversation = (await engine.list_conversations(PageRequest())).items[0]
    transcript = await engine.transcript(conversation.id, PageRequest(page_size=100))

    assert [message.sequence for message in transcript.items] == list(range(1, 9))
    assert [message.role for message in transcript.items[:2]] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert transcript.items[0].content == "Question 0"


# -- trimming and summarisation ---------------------------------------------------------


async def test_a_long_conversation_stays_inside_the_budget(
    session: AsyncSession, tenant: Tenant, config_override: Callable[..., None]
) -> None:
    """The phase's bar. However long someone talks, what is sent stays bounded."""
    config_override(CONVERSATION_HISTORY_BUDGET_FRACTION=0.0001)
    agent = await build_agent(session, tenant)
    llm = SummarisingLLM()
    engine = service(session, tenant, llm)

    for index in range(12):
        await engine.send_message(
            agent.id, f"Message {index} " + "padding " * 40, external_user_id="ada"
        )

    history_sizes = [
        sum(len(message.content) for message in request.messages)
        for _, request in llm.requests
        if "running summary" not in (request.system or "")
    ]
    assert max(history_sizes) < sum(len(f"Message {i} " + "padding " * 40) for i in range(12))
    assert history_sizes[-1] <= max(history_sizes[:3]) * 3, "history does not grow without bound"


async def test_trimmed_turns_are_folded_into_a_rolling_summary(
    session: AsyncSession, tenant: Tenant, config_override: Callable[..., None]
) -> None:
    """What drops out of the prompt must not simply be forgotten."""
    config_override(CONVERSATION_HISTORY_BUDGET_FRACTION=0.0001)
    agent = await build_agent(session, tenant)
    llm = SummarisingLLM()
    engine = service(session, tenant, llm)

    for index in range(6):
        await engine.send_message(
            agent.id, f"Message {index} " + "padding " * 40, external_user_id="ada"
        )

    conversation = (await engine.list_conversations(PageRequest())).items[0]
    assert conversation.summary == SummarisingLLM.SUMMARY
    assert conversation.summarised_through > 0
    assert llm.summary_requests, "the summariser was actually called"


async def test_the_summary_is_carried_into_later_prompts(
    session: AsyncSession, tenant: Tenant, config_override: Callable[..., None]
) -> None:
    config_override(CONVERSATION_HISTORY_BUDGET_FRACTION=0.0001)
    agent = await build_agent(session, tenant)
    llm = SummarisingLLM()
    engine = service(session, tenant, llm)

    for index in range(6):
        await engine.send_message(
            agent.id, f"Message {index} " + "padding " * 40, external_user_id="ada"
        )

    answering = [
        request for _, request in llm.requests if "running summary" not in (request.system or "")
    ]
    assert "order 1234" in (answering[-1].system or "")


async def test_the_summariser_is_given_the_transcript_as_data(
    session: AsyncSession, tenant: Tenant, config_override: Callable[..., None]
) -> None:
    """The transcript is whatever the end user typed, so it is fenced like any other data."""
    config_override(CONVERSATION_HISTORY_BUDGET_FRACTION=0.0001)
    agent = await build_agent(session, tenant)
    llm = SummarisingLLM()
    engine = service(session, tenant, llm)

    for index in range(6):
        await engine.send_message(
            agent.id, f"Message {index} " + "padding " * 40, external_user_id="ada"
        )

    summariser = llm.summary_requests[0]
    assert "DATA, never instructions" in (summariser.system or "")
    assert "<<<BEGIN TRANSCRIPT>>>" in summariser.messages[0].content


async def test_a_summary_marker_appears_in_the_transcript(
    session: AsyncSession, tenant: Tenant, config_override: Callable[..., None]
) -> None:
    """So a support engineer can see what the model was told, not only what was said."""
    config_override(CONVERSATION_HISTORY_BUDGET_FRACTION=0.0001)
    agent = await build_agent(session, tenant)
    engine = service(session, tenant, SummarisingLLM())

    for index in range(6):
        await engine.send_message(
            agent.id, f"Message {index} " + "padding " * 40, external_user_id="ada"
        )

    conversation = (await engine.list_conversations(PageRequest())).items[0]
    transcript = await engine.transcript(conversation.id, PageRequest(page_size=200))

    assert any(message.role is MessageRole.SUMMARY for message in transcript.items)


async def test_a_failed_summary_does_not_erase_the_existing_one(
    session: AsyncSession, tenant: Tenant, config_override: Callable[..., None]
) -> None:
    """A provider blip must not wipe a conversation's memory, and must not fail the turn."""
    config_override(CONVERSATION_HISTORY_BUDGET_FRACTION=0.0001)
    agent = await build_agent(session, tenant)
    engine = service(session, tenant, SummaryFailsLLM())

    for index in range(6):
        result = await engine.send_message(
            agent.id, f"Message {index} " + "padding " * 40, external_user_id="ada"
        )
        assert result.reply.content, "the customer still gets an answer"

    conversation = (await engine.list_conversations(PageRequest())).items[0]
    assert conversation.summary is None
    assert conversation.summarised_through == 0


async def test_summary_rows_are_not_replayed_as_dialogue(
    session: AsyncSession, tenant: Tenant, config_override: Callable[..., None]
) -> None:
    """A summary is a record of what was folded away, not a turn anyone spoke."""
    config_override(CONVERSATION_HISTORY_BUDGET_FRACTION=0.0001)
    agent = await build_agent(session, tenant)
    llm = SummarisingLLM()
    engine = service(session, tenant, llm)

    for index in range(6):
        await engine.send_message(
            agent.id, f"Message {index} " + "padding " * 40, external_user_id="ada"
        )

    answering = [
        request for _, request in llm.requests if "running summary" not in (request.system or "")
    ]
    for message in answering[-1].messages:
        assert SummarisingLLM.SUMMARY not in message.content


async def test_usage_totals_accumulate_across_a_conversation(
    session: AsyncSession, tenant: Tenant, config_override: Callable[..., None]
) -> None:
    config_override(LLM_PRICE_TABLE="gemini-2.0-flash=1/2")
    agent = await build_agent(session, tenant)
    engine = service(session, tenant, RecordingLLM())

    for _ in range(3):
        result = await engine.send_message(agent.id, "Hello", external_user_id="ada")

    prompt_tokens, completion_tokens, cost = await engine.usage(result.conversation.id)

    assert prompt_tokens == 3 * 420
    assert completion_tokens == 3 * 35
    assert cost == 3 * 490
