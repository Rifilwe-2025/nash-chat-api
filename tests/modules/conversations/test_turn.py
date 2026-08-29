"""A full turn, end to end (spec §5.4).

The phase's bar: user message → retrieval → prompt → provider → stored reply, for an agent with a
knowledge base attached. The provider is a recording fake — what is under test is *our* pipeline,
and a real call would make these tests slow, costly and non-deterministic while proving nothing
extra about the code in this repo.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.agents.domain.models import Agent, ModelProvider
from src.modules.agents.domain.services import AgentService
from src.modules.conversations.domain.models import (
    Channel,
    ConversationStatus,
    MessageRole,
)
from src.modules.conversations.domain.services import ConversationService
from src.modules.conversations.internal.prompt.delimiters import (
    KNOWLEDGE_CLOSE,
    KNOWLEDGE_OPEN,
    USER_OPEN,
)
from src.modules.knowledge_base.domain.services import KnowledgeBaseService
from src.modules.tenants.domain.models import Tenant
from src.shared.database.pagination import PageRequest
from src.shared.exceptions import ConflictException, ValidationException
from src.shared.llm import CompletionRequest, CompletionResult, TokenUsage
from src.shared.llm.errors import LLMUnavailableError

RETURNS = (
    "Paint may be returned within 30 days with a receipt. Tinted paint is mixed to order and is "
    "final sale, so it cannot be returned."
)


class RecordingLLM:
    """Stands in for ``LLMClient``, capturing every request the engine builds."""

    def __init__(self, reply: str = "Tinted paint is final sale, I'm afraid.") -> None:
        self.requests: list[tuple[str, CompletionRequest]] = []
        self.reply = reply

    async def complete(
        self, provider: str, request: CompletionRequest, api_key: str | None = None
    ) -> CompletionResult:
        self.requests.append((provider, request))
        return CompletionResult(
            content=self.reply,
            usage=TokenUsage(prompt_tokens=420, completion_tokens=35),
            model=request.model,
            provider=provider,
        )

    @property
    def last(self) -> CompletionRequest:
        return self.requests[-1][1]


class BrokenLLM(RecordingLLM):
    async def complete(
        self, provider: str, request: CompletionRequest, api_key: str | None = None
    ) -> CompletionResult:
        raise LLMUnavailableError("upstream is down", provider=provider)


@pytest.fixture
async def tenant(make_tenant: Callable[..., Coroutine[Any, Any, Tenant]]) -> Tenant:
    return await make_tenant(name="Nash Paints")


async def build_agent(
    session: AsyncSession, tenant: Tenant, published: bool = True, **config: Any
) -> Agent:
    service = AgentService(session, tenant.id)
    agent = await service.create(
        name=f"Agent {uuid.uuid4().hex[:6]}",
        persona="You are the sales assistant for Nash Paints.",
        model_provider=ModelProvider.GEMINI,
        model_settings={"model": "gemini-2.0-flash", "temperature": 0.4, "max_tokens": 512},
        **config,
    )
    if published:
        agent = await service.publish(agent.id)
    return agent


async def with_knowledge(session: AsyncSession, tenant: Tenant, agent: Agent) -> None:
    knowledge = KnowledgeBaseService(session, tenant.id)
    knowledge_base = await knowledge.create(name=f"Policies {uuid.uuid4().hex[:6]}")
    await knowledge.add_manual_source(knowledge_base.id, title="Returns", body=RETURNS)
    await knowledge.attach(knowledge_base.id, agent.id)


def service(session: AsyncSession, tenant: Tenant, llm: RecordingLLM) -> ConversationService:
    return ConversationService(session, tenant.id, llm_client=llm)  # type: ignore[arg-type]


# -- the happy path ------------------------------------------------------------------


async def test_a_full_turn_stores_both_sides(session: AsyncSession, tenant: Tenant) -> None:
    agent = await build_agent(session, tenant)
    await with_knowledge(session, tenant, agent)
    llm = RecordingLLM()

    result = await service(session, tenant, llm).send_message(
        agent.id, "Can I return tinted paint?"
    )

    assert result.reply.content == "Tinted paint is final sale, I'm afraid."
    assert result.reply.role is MessageRole.ASSISTANT
    assert result.user_message.content == "Can I return tinted paint?"
    assert result.conversation.status is ConversationStatus.ACTIVE


async def test_the_knowledge_base_reaches_the_prompt_fenced_as_data(
    session: AsyncSession, tenant: Tenant
) -> None:
    """The whole point of Phases 5 and 6 arriving here: the agent answers from its knowledge."""
    agent = await build_agent(session, tenant)
    await with_knowledge(session, tenant, agent)
    llm = RecordingLLM()

    await service(session, tenant, llm).send_message(agent.id, "Can I return tinted paint?")

    system = llm.last.system or ""
    assert "final sale" in system
    assert KNOWLEDGE_OPEN in system and KNOWLEDGE_CLOSE in system
    assert "Returns" in system, "the passage is attributed to its source"
    assert llm.last.messages[-1].content.startswith(USER_OPEN), "user input is fenced too"


async def test_the_agents_configured_model_and_sampling_are_used(
    session: AsyncSession, tenant: Tenant
) -> None:
    """Switching provider is a configuration change, never a code change (spec §10)."""
    agent = await build_agent(session, tenant)
    llm = RecordingLLM()

    await service(session, tenant, llm).send_message(agent.id, "Hello")

    provider, request = llm.requests[0]
    assert provider == "gemini"
    assert request.model == "gemini-2.0-flash"
    assert request.temperature == 0.4
    assert request.max_tokens == 512


async def test_token_usage_is_recorded_on_the_reply(session: AsyncSession, tenant: Tenant) -> None:
    agent = await build_agent(session, tenant)
    llm = RecordingLLM()

    result = await service(session, tenant, llm).send_message(agent.id, "Hello")

    assert result.reply.prompt_tokens == 420
    assert result.reply.completion_tokens == 35
    assert result.reply.total_tokens == 455


async def test_cost_is_recorded_when_a_price_is_configured(
    session: AsyncSession, tenant: Tenant, config_override: Callable[..., None]
) -> None:
    config_override(LLM_PRICE_TABLE="gemini-2.0-flash=1/2")
    agent = await build_agent(session, tenant)
    llm = RecordingLLM()

    result = await service(session, tenant, llm).send_message(agent.id, "Hello")

    # 420 in at $1/M plus 35 out at $2/M = $0.00049 = 490 micro-USD.
    assert result.reply.cost_micro_usd == 490


async def test_no_cost_is_invented_when_no_price_is_configured(
    session: AsyncSession, tenant: Tenant, config_override: Callable[..., None]
) -> None:
    config_override(LLM_PRICE_TABLE="")
    agent = await build_agent(session, tenant)

    result = await service(session, tenant, RecordingLLM()).send_message(agent.id, "Hello")

    assert result.reply.cost_micro_usd is None
    assert result.reply.prompt_tokens == 420, "tokens are measured either way"


async def test_the_answer_carries_its_citations(session: AsyncSession, tenant: Tenant) -> None:
    agent = await build_agent(session, tenant)
    await with_knowledge(session, tenant, agent)

    result = await service(session, tenant, RecordingLLM()).send_message(
        agent.id, "Can I return tinted paint?"
    )

    assert [item["sourceName"] for item in result.reply.citations_json] == ["Returns"]


async def test_an_agent_with_no_knowledge_is_told_so(session: AsyncSession, tenant: Tenant) -> None:
    """Not silence: an agent that is not told the knowledge base was empty will fill the gap."""
    agent = await build_agent(session, tenant)
    llm = RecordingLLM()

    result = await service(session, tenant, llm).send_message(agent.id, "Anything?")

    assert "No relevant information was found" in (llm.last.system or "")
    assert result.retrieval is not None
    assert result.retrieval.has_context is False


# -- sessions -------------------------------------------------------------------------


async def test_the_open_session_is_continued_across_messages(
    session: AsyncSession, tenant: Tenant
) -> None:
    agent = await build_agent(session, tenant)
    engine = service(session, tenant, RecordingLLM())

    first = await engine.send_message(agent.id, "Hello", external_user_id="ada")
    second = await engine.send_message(agent.id, "Are you there?", external_user_id="ada")

    assert first.conversation.id == second.conversation.id


async def test_different_users_get_different_conversations(
    session: AsyncSession, tenant: Tenant
) -> None:
    agent = await build_agent(session, tenant)
    engine = service(session, tenant, RecordingLLM())

    ada = await engine.send_message(agent.id, "Hello", external_user_id="ada")
    grace = await engine.send_message(agent.id, "Hello", external_user_id="grace")

    assert ada.conversation.id != grace.conversation.id


async def test_history_is_carried_into_the_next_turn(session: AsyncSession, tenant: Tenant) -> None:
    agent = await build_agent(session, tenant)
    llm = RecordingLLM()
    engine = service(session, tenant, llm)

    await engine.send_message(agent.id, "My order number is 1234", external_user_id="ada")
    await engine.send_message(agent.id, "What was my order number?", external_user_id="ada")

    contents = [message.content for message in llm.last.messages]
    assert any("1234" in content for content in contents)


async def test_a_closed_conversation_starts_a_fresh_one(
    session: AsyncSession, tenant: Tenant
) -> None:
    agent = await build_agent(session, tenant)
    engine = service(session, tenant, RecordingLLM())
    first = await engine.send_message(agent.id, "Hello", external_user_id="ada")

    await engine.close(first.conversation.id)
    second = await engine.send_message(agent.id, "Hello again", external_user_id="ada")

    assert second.conversation.id != first.conversation.id


async def test_writing_into_a_closed_conversation_by_id_is_refused(
    session: AsyncSession, tenant: Tenant
) -> None:
    agent = await build_agent(session, tenant)
    engine = service(session, tenant, RecordingLLM())
    first = await engine.send_message(agent.id, "Hello")
    await engine.close(first.conversation.id)

    with pytest.raises(ConflictException) as caught:
        await engine.send_message(agent.id, "More", conversation_id=first.conversation.id)

    assert caught.value.code == "CONVERSATION_NOT_ACTIVE"


# -- guardrails ------------------------------------------------------------------------


async def test_an_escalation_trigger_hands_over_without_calling_the_model(
    session: AsyncSession, tenant: Tenant
) -> None:
    """The model is the component under attack; it must not be able to talk itself out of a
    handoff (spec §5.7)."""
    agent = await build_agent(
        session,
        tenant,
        engagement_rules={"escalation_triggers": ["speak to a manager"]},
    )
    llm = RecordingLLM()

    result = await service(session, tenant, llm).send_message(
        agent.id, "Please let me speak to a manager"
    )

    assert result.escalated is True
    assert result.conversation.status is ConversationStatus.ESCALATED
    assert result.conversation.escalation_reason
    assert llm.requests == [], "no provider call is made for an escalation"


async def test_an_escalated_conversation_stops_being_the_open_session(
    session: AsyncSession, tenant: Tenant
) -> None:
    """Otherwise the agent talks over whoever picked the conversation up."""
    agent = await build_agent(
        session, tenant, engagement_rules={"escalation_triggers": ["manager"]}
    )
    engine = service(session, tenant, RecordingLLM())
    escalated = await engine.send_message(agent.id, "Get me a manager", external_user_id="ada")

    following = await engine.send_message(agent.id, "Hello?", external_user_id="ada")

    assert following.conversation.id != escalated.conversation.id


async def test_a_restricted_topic_is_declined_without_a_provider_call(
    session: AsyncSession, tenant: Tenant
) -> None:
    agent = await build_agent(
        session,
        tenant,
        guardrails={
            "restricted_topics": ["legal advice"],
            "fallback_response": "I can't advise on that.",
        },
    )
    llm = RecordingLLM()

    result = await service(session, tenant, llm).send_message(
        agent.id, "Can you give me legal advice?"
    )

    assert result.reply.content == "I can't advise on that."
    assert result.escalated is False
    assert llm.requests == []


# -- failure and validation --------------------------------------------------------------


async def test_a_provider_outage_is_reported_without_leaking_internals(
    session: AsyncSession, tenant: Tenant
) -> None:
    agent = await build_agent(session, tenant)

    with pytest.raises(ConflictException) as caught:
        await service(session, tenant, BrokenLLM()).send_message(agent.id, "Hello")

    assert caught.value.code == "PROVIDER_UNAVAILABLE"
    assert "upstream is down" not in str(caught.value.detail or "")


async def test_an_unconfigured_agent_cannot_be_talked_to(
    session: AsyncSession, tenant: Tenant
) -> None:
    agent = await AgentService(session, tenant.id).create(name="Bare")

    with pytest.raises(ValidationException) as caught:
        await service(session, tenant, RecordingLLM()).send_message(agent.id, "Hello")

    assert caught.value.code == "AGENT_NOT_CONFIGURED"


async def test_a_draft_agent_can_be_previewed_but_not_served(
    session: AsyncSession, tenant: Tenant
) -> None:
    """Journey step 3: testing a draft in the builder is exactly what preview is for."""
    agent = await build_agent(session, tenant, published=False)
    engine = service(session, tenant, RecordingLLM())

    preview = await engine.send_message(agent.id, "Hello", channel=Channel.PREVIEW)
    assert preview.reply.content

    with pytest.raises(ConflictException) as caught:
        await engine.send_message(agent.id, "Hello", channel=Channel.WEB)
    assert caught.value.code == "AGENT_NOT_PUBLISHED"


async def test_a_paused_agent_stops_serving_real_traffic(
    session: AsyncSession, tenant: Tenant
) -> None:
    agent = await build_agent(session, tenant)
    await AgentService(session, tenant.id).pause(agent.id)

    with pytest.raises(ConflictException):
        await service(session, tenant, RecordingLLM()).send_message(
            agent.id, "Hello", channel=Channel.WEB
        )


@pytest.mark.parametrize("message", ["", "   ", "\n\t "])
async def test_an_empty_message_is_refused(
    session: AsyncSession, tenant: Tenant, message: str
) -> None:
    agent = await build_agent(session, tenant)

    with pytest.raises(ValidationException) as caught:
        await service(session, tenant, RecordingLLM()).send_message(agent.id, message)

    assert caught.value.code == "EMPTY_MESSAGE"


async def test_an_oversized_message_is_refused(
    session: AsyncSession, tenant: Tenant, config_override: Callable[..., None]
) -> None:
    config_override(CONVERSATION_MAX_MESSAGE_CHARACTERS=50)
    agent = await build_agent(session, tenant)

    with pytest.raises(ValidationException) as caught:
        await service(session, tenant, RecordingLLM()).send_message(agent.id, "x" * 51)

    assert caught.value.code == "MESSAGE_TOO_LONG"


# -- isolation ---------------------------------------------------------------------------


async def test_another_tenants_agent_cannot_be_talked_to(
    session: AsyncSession, make_tenant: Callable[..., Coroutine[Any, Any, Tenant]]
) -> None:
    first = await make_tenant(name="Tenant A")
    second = await make_tenant(name="Tenant B")
    theirs = await build_agent(session, first)

    with pytest.raises(Exception) as caught:
        await service(session, second, RecordingLLM()).send_message(theirs.id, "Hello")

    assert getattr(caught.value, "code", "") == "AGENT_NOT_FOUND"


async def test_conversations_are_listed_only_within_a_tenant(
    session: AsyncSession, make_tenant: Callable[..., Coroutine[Any, Any, Tenant]]
) -> None:
    first = await make_tenant(name="Tenant A")
    second = await make_tenant(name="Tenant B")
    theirs = await build_agent(session, first)
    await service(session, first, RecordingLLM()).send_message(theirs.id, "Hello")

    listed = await service(session, second, RecordingLLM()).list_conversations(PageRequest())

    assert listed.total == 0


async def test_the_agent_status_check_does_not_reveal_another_tenants_agent(
    session: AsyncSession, make_tenant: Callable[..., Coroutine[Any, Any, Tenant]]
) -> None:
    """A foreign agent must read as missing, never as "exists but is paused"."""
    first = await make_tenant(name="Tenant A")
    second = await make_tenant(name="Tenant B")
    theirs = await build_agent(session, first, published=False)

    with pytest.raises(Exception) as caught:
        await service(session, second, RecordingLLM()).send_message(
            theirs.id, "Hello", channel=Channel.WEB
        )

    assert getattr(caught.value, "code", "") == "AGENT_NOT_FOUND"
