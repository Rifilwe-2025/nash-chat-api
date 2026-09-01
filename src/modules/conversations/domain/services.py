"""The conversation engine: one user message in, one stored reply out (spec §5.4).

``send_message`` is the whole phase. In order, a turn:

1. **takes the session lock**, so rapid messages in one conversation are answered in order;
2. **checks guardrails in code** — escalation and restricted topics are decided before the model is
   involved, because a conversation must not be able to argue its way out of a handoff (§5.7);
3. **retrieves** through the knowledge base service, which picks its own tier;
4. **assembles the prompt** — persona and guardrails as instructions, knowledge and the user's words
   fenced as data;
5. **trims history** to the budget, folding whatever it drops into the rolling summary;
6. **calls the provider** through the Phase 4 abstraction, so the agent's configured model is a
   configuration lookup and nothing here knows which vendor answered;
7. **stores both turns** with the measured token usage and, where a price is configured, the cost.

Cross-module access is service → service throughout: agents and knowledge come from
``AgentService`` and ``KnowledgeBaseService``, never from their repositories or models.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src import configs
from src.modules.agents.domain.models import Agent, AgentStatus
from src.modules.agents.domain.services import AgentService
from src.modules.conversations.domain.models import (
    Channel,
    Conversation,
    ConversationStatus,
    Message,
    MessageRole,
)
from src.modules.conversations.domain.repositories import (
    ConversationRepository,
    MessageRepository,
)
from src.modules.conversations.internal import guardrails, tool_loop
from src.modules.conversations.internal.history.summarisation import summarise
from src.modules.conversations.internal.history.trimming import (
    HistoryTurn,
    history_budget,
    trim,
)
from src.modules.conversations.internal.locking import lock_conversation
from src.modules.conversations.internal.prompt.assembly import AgentPrompt, build_system_prompt
from src.modules.conversations.internal.prompt.delimiters import fence_user_message
from src.modules.knowledge_base.domain.services import KnowledgeBaseService, RetrievalResult
from src.modules.tools.domain.services import ResponseCache, ToolResult, ToolService
from src.shared.database.pagination import Page, PageRequest
from src.shared.exceptions import ConflictException, NotFoundException, ValidationException
from src.shared.llm import (
    ChatMessage,
    CompletionRequest,
    CompletionResult,
    LLMClient,
    LLMError,
    Role,
)
from src.shared.llm.context import context_characters
from src.shared.llm.pricing import cost_micro_usd

logger = logging.getLogger("api.conversations")

DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.7


class TurnResult:
    """One completed turn, as the caller sees it."""

    def __init__(
        self,
        conversation: Conversation,
        user_message: Message,
        reply: Message,
        retrieval: RetrievalResult | None,
        escalated: bool = False,
        tool_calls: list[ToolResult] | None = None,
    ) -> None:
        self.conversation = conversation
        self.user_message = user_message
        self.reply = reply
        self.retrieval = retrieval
        self.escalated = escalated
        # What the turn looked up, if anything. Channels do not use it yet; the conversation API
        # surfaces it so a tenant can see which answers came from a live call.
        self.tool_calls = tool_calls or []


class ConversationService:
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        llm_client: LLMClient | None = None,
        tool_cache: ResponseCache | None = None,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.conversations = ConversationRepository(session, tenant_id)
        self.messages = MessageRepository(session)
        self.agents = AgentService(session, tenant_id)
        self.knowledge = KnowledgeBaseService(session, tenant_id)
        # Service to service, like knowledge: the turn asks the tools module what this agent may
        # call and to run one, and never touches a tool row or the HTTP client itself.
        self.tools = ToolService(session, tenant_id, cache=tool_cache)
        self._llm = llm_client or LLMClient()

    # -- reads ---------------------------------------------------------------

    async def get(self, conversation_id: uuid.UUID) -> Conversation:
        conversation = await self.conversations.get(conversation_id)
        if conversation is None:
            raise NotFoundException("Conversation does not exist.", code="CONVERSATION_NOT_FOUND")
        return conversation

    async def list_conversations(
        self,
        page: PageRequest,
        agent_id: uuid.UUID | None = None,
        status: ConversationStatus | None = None,
    ) -> Page[Conversation]:
        if agent_id is not None:
            await self.agents.get(agent_id)  # 404s a foreign agent before listing anything
        return await self.conversations.list_conversations(page, agent_id=agent_id, status=status)

    async def transcript(self, conversation_id: uuid.UUID, page: PageRequest) -> Page[Message]:
        await self.get(conversation_id)
        return await self.messages.transcript(conversation_id, page)

    async def usage(self, conversation_id: uuid.UUID) -> tuple[int, int, int]:
        await self.get(conversation_id)
        return await self.messages.usage_totals(conversation_id)

    # -- lifecycle -----------------------------------------------------------

    async def close(self, conversation_id: uuid.UUID) -> Conversation:
        conversation = await self.get(conversation_id)
        if conversation.status is ConversationStatus.CLOSED:
            return conversation
        return await self.conversations.update(conversation, status=ConversationStatus.CLOSED)

    async def escalate(self, conversation_id: uuid.UUID, reason: str | None = None) -> Conversation:
        """Hand the conversation to a human.

        The handoff *event* has nowhere to go until a channel exists (Phases 8 and 10), so it is
        logged and recorded on the row. What matters now is that the agent stops answering: an
        escalated conversation is no longer an open session, so the next message starts a fresh one
        rather than the agent talking over the human.
        """
        conversation = await self.get(conversation_id)
        return await self._escalate(conversation, reason or "Escalated by the tenant.")

    # -- the turn ------------------------------------------------------------

    async def send_message(
        self,
        agent_id: uuid.UUID,
        content: str,
        channel: Channel = Channel.PREVIEW,
        external_user_id: str = "preview",
        conversation_id: uuid.UUID | None = None,
    ) -> TurnResult:
        """Run one full turn and store both sides of it."""
        message = self._validated(content)
        agent = await self._agent_for_turn(agent_id, channel)
        conversation = await self._session_for(agent, channel, external_user_id, conversation_id)

        # Everything below reads and writes this conversation's history, so nothing else may be
        # doing the same at the same time (see internal/locking.py).
        await lock_conversation(self.session, conversation.id)

        user_message = await self._store(conversation, MessageRole.USER, message)

        decision = guardrails.evaluate(
            message,
            escalation_triggers=self._rules(agent, "escalation_triggers"),
            restricted_topics=self._guardrails(agent, "restricted_topics"),
        )

        if decision.action is guardrails.GuardrailAction.ESCALATE:
            reply = await self._store(
                conversation,
                MessageRole.ASSISTANT,
                guardrails.escalation_response(),
                meta={"guardrail": "escalated", "matched": decision.matched},
            )
            await self._escalate(conversation, decision.reason or "Escalation trigger matched.")
            return TurnResult(conversation, user_message, reply, retrieval=None, escalated=True)

        if decision.action is guardrails.GuardrailAction.DECLINE:
            # Never reaches the provider: a restricted topic is settled, and paying a model to
            # decline something we already decided to decline would be waste.
            reply = await self._store(
                conversation,
                MessageRole.ASSISTANT,
                guardrails.decline_response(self._fallback(agent)),
                meta={"guardrail": "declined", "matched": decision.matched},
            )
            return TurnResult(conversation, user_message, reply, retrieval=None)

        return await self._answer(agent, conversation, user_message, message)

    async def stream_message(
        self,
        agent_id: uuid.UUID,
        content: str,
        channel: Channel = Channel.PREVIEW,
        external_user_id: str = "preview",
    ) -> tuple[Conversation, AsyncIterator[str]]:
        """Run a turn, yielding the reply as it is written.

        Returns the conversation *and* an iterator, rather than being a generator itself, so the
        caller has the conversation id before the first token — a widget needs it to attach the
        stream to the right thread.

        A guardrail reply is not streamed from anywhere: the answer was decided in code, so it
        arrives as one chunk. The caller cannot tell the difference, which is the point.

        **Streamed turns record no token usage.** The providers' streaming APIs do not report it,
        and the abstraction will not invent numbers it was not given. The message is stored with a
        ``streamed`` marker so analytics can tell why the counts are zero rather than reading it as
        a free call.
        """
        message = self._validated(content)
        agent = await self._agent_for_turn(agent_id, channel)
        conversation = await self._session_for(agent, channel, external_user_id, None)
        await lock_conversation(self.session, conversation.id)

        user_message = await self._store(conversation, MessageRole.USER, message)

        decision = guardrails.evaluate(
            message,
            escalation_triggers=self._rules(agent, "escalation_triggers"),
            restricted_topics=self._guardrails(agent, "restricted_topics"),
        )
        if decision.action is not guardrails.GuardrailAction.ALLOW:
            escalating = decision.action is guardrails.GuardrailAction.ESCALATE
            text = (
                guardrails.escalation_response()
                if escalating
                else guardrails.decline_response(self._fallback(agent))
            )
            await self._store(
                conversation,
                MessageRole.ASSISTANT,
                text,
                meta={"guardrail": decision.action.value, "matched": decision.matched},
            )
            if escalating:
                await self._escalate(conversation, decision.reason or "Escalation trigger matched.")
            return conversation, _single_chunk(text)

        request, provider, api_key, retrieval, history_turns = await self._prepare(
            agent, conversation, message
        )

        if request.tools:
            # A stream cannot pause mid-token to make an HTTP call and resume, so an agent with
            # tools takes the buffered path and its finished answer is delivered as one chunk —
            # exactly what a guardrail reply already does above. The caller cannot tell the
            # difference, and the alternative is worse either way: offering tools to a streaming
            # call means a stream that ends with no text when the model chooses one, and dropping
            # the tools means a published agent that silently loses its lookups on the widget.
            result = await self._answer(agent, conversation, user_message, message)
            return conversation, _single_chunk(result.reply.content)

        return conversation, self._stream_and_store(
            conversation, provider, api_key, request, retrieval, history_turns
        )

    async def _stream_and_store(
        self,
        conversation: Conversation,
        provider: str,
        api_key: str | None,
        request: CompletionRequest,
        retrieval: RetrievalResult,
        history_turns: int,
    ) -> AsyncIterator[str]:
        """Yield deltas, then store the assembled reply once the stream ends."""
        pieces: list[str] = []
        try:
            async for delta in self._llm.stream(provider, request, api_key=api_key):
                pieces.append(delta)
                yield delta
        except LLMError as exc:
            # Bytes already sent cannot be un-sent, so this cannot become a 4xx. The client is told
            # in band, and the partial text is still stored — a half answer in the transcript is
            # more use to whoever investigates than a gap.
            logger.warning("streamed provider call failed: %s", exc)
            yield "\n\n[The reply was interrupted. Please try again.]"

        text = "".join(pieces).strip()
        if text:
            await self._store(
                conversation,
                MessageRole.ASSISTANT,
                text,
                provider=provider,
                model=request.model,
                citations=self._citations(retrieval),
                meta={
                    "tier": retrieval.tier.value,
                    "hasContext": retrieval.has_context,
                    "historyTurns": history_turns,
                    "streamed": True,
                },
            )

    def _validated(self, content: str) -> str:
        message = content.strip()
        if not message:
            raise ValidationException("A message cannot be empty.", code="EMPTY_MESSAGE")
        limit: int = configs.CONVERSATIONS_MAX_MESSAGE_CHARACTERS
        if len(message) > limit:
            raise ValidationException(
                f"A message may be at most {limit} characters.", code="MESSAGE_TOO_LONG"
            )
        return message

    # -- internals -----------------------------------------------------------

    async def _prepare(
        self, agent: Agent, conversation: Conversation, message: str
    ) -> tuple[CompletionRequest, str, str | None, RetrievalResult, int]:
        """Everything a turn needs before the provider is called.

        Shared by the buffered and streamed paths so the two cannot drift: a streamed reply must be
        produced from exactly the same prompt, knowledge and history as a buffered one.

        The agent's own provider key comes back beside the provider name and is threaded through
        every call this turn makes — the first completion, each round of the tool loop, and the
        summariser. Carrying it here rather than resolving it per call site is what keeps a turn
        from silently billing half of itself to the platform's key.
        """
        model = str(agent.model_config_json.get("model") or "")
        provider = agent.model_provider.value if agent.model_provider else ""

        retrieval = await self.knowledge.retrieve(message, agent_id=agent.id, model=model)
        # An agent with no tools gets an empty list, and everything downstream — the prompt note,
        # the loop, the follow-up call — is skipped. A KB-only agent costs exactly what it did
        # before this phase.
        tools = await self.tools.definitions_for(agent.id)

        system_prompt = build_system_prompt(
            self._agent_prompt(agent),
            passages=[
                (passage.citation.source_name, passage.text) for passage in retrieval.passages
            ],
            has_context=retrieval.has_context,
            history_summary=conversation.summary,
            has_tools=bool(tools),
        )

        turns = await self._history_for_prompt(conversation, system_prompt, agent, model)

        request = CompletionRequest(
            messages=[
                *(ChatMessage(role=Role(turn.role), content=turn.content) for turn in turns),
                ChatMessage(role=Role.USER, content=fence_user_message(message)),
            ],
            model=model,
            system=system_prompt,
            max_tokens=int(agent.model_config_json.get("max_tokens") or DEFAULT_MAX_TOKENS),
            temperature=float(agent.model_config_json.get("temperature", DEFAULT_TEMPERATURE)),
            tools=tools,
        )
        return request, provider, agent.model_api_key, retrieval, len(turns)

    def _citations(self, retrieval: RetrievalResult) -> list[Any]:
        return [
            {
                "sourceId": str(citation.source_id),
                "kbId": str(citation.kb_id),
                "sourceName": citation.source_name,
            }
            for citation in retrieval.citations
        ]

    async def _answer(
        self,
        agent: Agent,
        conversation: Conversation,
        user_message: Message,
        message: str,
    ) -> TurnResult:
        request, provider, api_key, retrieval, history_turns = await self._prepare(
            agent, conversation, message
        )

        try:
            result = await self._llm.complete(provider, request, api_key=api_key)
            outcome = await self._resolve_tools(
                agent, conversation, provider, api_key, request, result
            )
        except LLMError as exc:
            logger.warning("provider call failed for agent %s: %s", agent.id, exc)
            raise ConflictException(
                "The agent's model could not be reached. Please try again.",
                code="PROVIDER_UNAVAILABLE",
                message="The agent is temporarily unavailable.",
            ) from exc

        result = outcome.result
        meta: dict[str, Any] = {
            "tier": retrieval.tier.value,
            "hasContext": retrieval.has_context,
            "historyTurns": history_turns,
        }
        if outcome.used_tools:
            meta["toolCalls"] = outcome.summary()
            meta["toolRounds"] = outcome.rounds

        reply = await self._store(
            conversation,
            MessageRole.ASSISTANT,
            result.content.strip(),
            provider=result.provider,
            model=result.model,
            # The whole turn's usage, summed across every provider call the tool loop made —
            # not just the final one. A tool-using turn calls the model at least twice and the
            # tenant pays for both.
            prompt_tokens=outcome.usage.prompt_tokens,
            completion_tokens=outcome.usage.completion_tokens,
            citations=self._citations(retrieval),
            meta=meta,
        )
        return TurnResult(
            conversation, user_message, reply, retrieval=retrieval, tool_calls=outcome.calls
        )

    async def _resolve_tools(
        self,
        agent: Agent,
        conversation: Conversation,
        provider: str,
        api_key: str | None,
        request: CompletionRequest,
        first: CompletionResult,
    ) -> tool_loop.ToolLoopOutcome:
        """Run whatever the model asked for, and get its final answer.

        Returns immediately when nothing was asked for, which is the common case and must stay
        free. The loop itself is in ``internal/tool_loop.py``; this is the seam that knows the
        agent, the conversation and the budget.
        """
        if not first.tool_calls:
            # `usage` is carried explicitly. An outcome built without it reports zero tokens, which
            # would silently zero the accounting on every turn that used no tool — that is, almost
            # all of them.
            return tool_loop.ToolLoopOutcome(result=first, usage=first.usage)

        return await tool_loop.run(
            self._llm,
            provider,
            request,
            first,
            api_key=api_key,
            tools=self.tools,
            agent_id=agent.id,
            conversation_id=conversation.id,
            max_calls=await self.tools.max_calls_per_turn(agent.id),
        )

    async def _history_for_prompt(
        self,
        conversation: Conversation,
        system_prompt: str,
        agent: Agent,
        model: str,
    ) -> list[HistoryTurn]:
        """Trim history to the budget, folding whatever falls out into the rolling summary."""
        stored = await self.messages.history(conversation.id)
        # The turn just stored is sent separately as the live message, so it is not history yet.
        turns = [HistoryTurn(role=item.role.value, content=item.content) for item in stored[:-1]]

        budget = history_budget(
            context_characters=context_characters(model),
            system_prompt_characters=len(system_prompt),
            max_output_tokens=int(agent.model_config_json.get("max_tokens") or DEFAULT_MAX_TOKENS),
            reserve_fraction=configs.CONVERSATIONS_HISTORY_BUDGET_FRACTION,
        )
        result = trim(turns, budget)

        if result.dropped:
            await self._fold_into_summary(conversation, result.dropped, agent, model)
        return result.kept

    async def _fold_into_summary(
        self,
        conversation: Conversation,
        dropped: list[HistoryTurn],
        agent: Agent,
        model: str,
    ) -> None:
        """Summarise the turns leaving the prompt, so what they contained is not lost."""
        already = conversation.summarised_through
        fresh = dropped[already:]
        if not fresh:
            return

        summary = await summarise(
            self._llm,
            provider=agent.model_provider.value if agent.model_provider else "",
            model=model,
            previous_summary=conversation.summary,
            transcript=[(turn.role, turn.content) for turn in fresh],
            max_tokens=configs.CONVERSATIONS_SUMMARY_MAX_TOKENS,
            api_key=agent.model_api_key,
        )
        if summary is None:
            return  # provider blip: keep the summary we had rather than erasing it

        await self.conversations.update(
            conversation, summary=summary, summarised_through=len(dropped)
        )
        await self.messages.add(
            Message(
                conversation_id=conversation.id,
                sequence=await self.messages.next_sequence(conversation.id),
                role=MessageRole.SUMMARY,
                content=summary,
                meta_json={"foldedTurns": len(fresh)},
            )
        )

    async def _agent_for_turn(self, agent_id: uuid.UUID, channel: Channel) -> Agent:
        """Load the agent and check it may serve this channel.

        The builder's preview is how a draft gets tested before publishing (§5.1, journey step 3),
        so preview works at any status. Real traffic requires a published agent — a paused agent has
        been deliberately taken out of service.
        """
        agent = await self.agents.get(agent_id)

        if agent.model_provider is None or not agent.model_config_json.get("model"):
            raise ValidationException(
                "This agent has no model configured yet.", code="AGENT_NOT_CONFIGURED"
            )
        if channel is not Channel.PREVIEW and agent.status is not AgentStatus.PUBLISHED:
            raise ConflictException(
                f"This agent is {agent.status.value} and is not serving traffic.",
                code="AGENT_NOT_PUBLISHED",
            )
        return agent

    async def _session_for(
        self,
        agent: Agent,
        channel: Channel,
        external_user_id: str,
        conversation_id: uuid.UUID | None,
    ) -> Conversation:
        """Resolve the conversation this message belongs to, creating one if there is none."""
        if conversation_id is not None:
            conversation = await self.get(conversation_id)
            if conversation.agent_id != agent.id:
                raise NotFoundException(
                    "Conversation does not exist.", code="CONVERSATION_NOT_FOUND"
                )
            if conversation.status is not ConversationStatus.ACTIVE:
                raise ConflictException(
                    f"This conversation is {conversation.status.value}.",
                    code="CONVERSATION_NOT_ACTIVE",
                )
            return conversation

        existing = await self.conversations.find_open_session(agent.id, channel, external_user_id)
        if existing is not None:
            return existing

        return await self.conversations.add(
            Conversation(
                agent_id=agent.id,
                channel=channel,
                external_user_id=external_user_id,
                status=ConversationStatus.ACTIVE,
            )
        )

    async def _escalate(self, conversation: Conversation, reason: str) -> Conversation:
        if conversation.status is ConversationStatus.ESCALATED:
            return conversation
        logger.info(
            "handoff requested: conversation=%s agent=%s tenant=%s reason=%s",
            conversation.id,
            conversation.agent_id,
            self.tenant_id,
            reason,
        )
        return await self.conversations.update(
            conversation,
            status=ConversationStatus.ESCALATED,
            escalated_at=datetime.now(UTC),
            escalation_reason=reason[:500],
        )

    async def _store(
        self,
        conversation: Conversation,
        role: MessageRole,
        content: str,
        provider: str | None = None,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        citations: list[Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Message:
        message = await self.messages.add(
            Message(
                conversation_id=conversation.id,
                sequence=await self.messages.next_sequence(conversation.id),
                role=role,
                content=content,
                provider=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_micro_usd=cost_micro_usd(model, prompt_tokens, completion_tokens),
                citations_json=citations or [],
                meta_json=meta or {},
            )
        )
        await self.conversations.update(conversation, last_message_at=datetime.now(UTC))
        return message

    # -- agent configuration readers -----------------------------------------

    def _agent_prompt(self, agent: Agent) -> AgentPrompt:
        rules = agent.engagement_rules or {}
        rails = agent.guardrails or {}
        return AgentPrompt(
            persona=agent.persona or "",
            tone=rules.get("tone"),
            style=rules.get("style"),
            dos=list(rules.get("dos") or []),
            donts=list(rules.get("donts") or []),
            restricted_topics=list(rails.get("restricted_topics") or []),
            fallback_response=rails.get("fallback_response"),
            require_grounded_answers=bool(rails.get("require_grounded_answers", True)),
        )

    def _rules(self, agent: Agent, key: str) -> list[str]:
        return [str(item) for item in (agent.engagement_rules or {}).get(key) or []]

    def _guardrails(self, agent: Agent, key: str) -> list[str]:
        return [str(item) for item in (agent.guardrails or {}).get(key) or []]

    def _fallback(self, agent: Agent) -> str | None:
        value = (agent.guardrails or {}).get("fallback_response")
        return str(value) if value else None


async def _single_chunk(text: str) -> AsyncIterator[str]:
    """A guardrail reply, in the shape a streamed one arrives in."""
    yield text
