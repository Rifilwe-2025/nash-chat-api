"""Aggregate reads for the analytics module — every ``select(...)`` it makes lives here.

Analytics is the one module that reads other modules' tables. The plan sanctions that (a read model
needs dedicated read repositories rather than a service call per row), but it makes tenant isolation
the thing most likely to go wrong here of anywhere in the codebase: a missing ``tenant_id`` in one
``GROUP BY`` would show one tenant another's traffic, and nothing about the response would look
wrong (spec §5.7).

So none of these classes filters by tenant itself. Each one extends
:class:`~src.shared.database.repository.TenantScopedRepository` over a model that *is*
tenant-scoped, and every query is built from ``self._base_query()`` — the same scoped starting
point the owning module uses. Rows that are not tenant-scoped (``message``, ``tool_call``) are only
ever reached by joining a scoped subquery of their parent, never selected from directly. Isolation
is inherited rather than re-implemented, which is the only version of it that stays true.

Nothing here writes to another module's tables. The only insert in this module is its own
:class:`~src.modules.analytics.domain.models.PlatformEvent`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Select, func, select

from src.modules.agents.domain.models import Agent
from src.modules.analytics.domain.models import EventCategory, PlatformEvent
from src.modules.analytics.internal.windows import Window
from src.modules.channels.domain.models import WebhookEndpoint, WebhookStatus
from src.modules.channels.whatsapp.domain.models import DeliveryStatus, WhatsAppMessage
from src.modules.conversations.domain.models import (
    Channel,
    Conversation,
    ConversationStatus,
    Message,
    MessageRole,
)
from src.modules.knowledge_base.domain.models import KbSource, SourceStatus
from src.modules.tools.domain.models import AgentTool, ToolCallLog, ToolOutcome
from src.shared.database.repository import TenantScopedRepository


@dataclass(frozen=True, slots=True)
class ConversationCounts:
    started: int
    escalated: int
    open_now: int


@dataclass(frozen=True, slots=True)
class MessageTotals:
    """One window's traffic and what it cost.

    ``cost_micro_usd`` sums only the messages whose model has a configured price
    (``shared/llm/pricing.py``); ``priced_messages`` says how many those were, so a reader can tell
    "this cost nothing" from "we do not know what this cost".
    """

    total: int
    user: int
    assistant: int
    prompt_tokens: int
    completion_tokens: int
    cost_micro_usd: int
    priced_messages: int
    without_context: int
    declined: int


@dataclass(frozen=True, slots=True)
class DailyPoint:
    day: date
    messages: int
    conversations: int
    tokens: int
    cost_micro_usd: int


@dataclass(frozen=True, slots=True)
class ChannelPoint:
    channel: Channel
    conversations: int
    messages: int


@dataclass(frozen=True, slots=True)
class ModelPoint:
    provider: str | None
    model: str | None
    messages: int
    prompt_tokens: int
    completion_tokens: int
    cost_micro_usd: int


@dataclass(frozen=True, slots=True)
class FailureItem:
    """One failure, in the shape the report renders whichever table it came from."""

    occurred_at: datetime
    kind: str
    code: str
    detail: str | None
    subject: str
    subject_id: uuid.UUID
    agent_id: uuid.UUID | None = None


class UsageRepository(TenantScopedRepository[Conversation]):
    """Message and conversation aggregates for one tenant.

    ``message`` is not tenant-scoped — it is reached through its conversation, which is — so every
    message query here joins the scoped conversation subquery rather than selecting from ``message``
    on its own.

    **Preview traffic is excluded by default.** The builder's test chat is the tenant trying their
    own agent out (``Channel.PREVIEW``); counting it as customer traffic would inflate every number
    on the dashboard and make the cost figure wrong in the direction that matters.
    """

    model = Conversation

    def _conversation_scope(
        self, agent_id: uuid.UUID | None, include_preview: bool
    ) -> Select[tuple[Conversation]]:
        query = self._base_query()
        if agent_id is not None:
            query = query.where(Conversation.agent_id == agent_id)
        if not include_preview:
            query = query.where(Conversation.channel != Channel.PREVIEW)
        return query

    def _messages_in(
        self, window: Window, agent_id: uuid.UUID | None, include_preview: bool
    ) -> Select[Any]:
        """Messages in the window, already narrowed to this tenant through their conversation.

        Returns the joined ``FROM`` rather than a subquery of ids: every aggregate below adds its
        own columns to this, and wrapping it first would leave those columns referring to the
        ``message`` table outside the subquery — a cartesian product that still returns a number,
        just the wrong one.
        """
        conversations = self._conversation_scope(agent_id, include_preview).subquery()
        return (
            select()
            .select_from(Message)
            .join(conversations, Message.conversation_id == conversations.c.id)
            .where(Message.created_at >= window.start, Message.created_at < window.end)
        )

    async def conversation_counts(
        self, window: Window, agent_id: uuid.UUID | None, include_preview: bool
    ) -> ConversationCounts:
        """Started, escalated, and still open.

        ``started`` and ``escalated`` are both counted by *when they happened*, so both belong to
        the window in the same way. ``open_now`` deliberately is not: how many conversations are
        open is a fact about this moment, not about the period, and dating it would make it
        meaningless.
        """
        scope = self._conversation_scope(agent_id, include_preview)

        started = await self._count(
            scope.where(
                Conversation.created_at >= window.start, Conversation.created_at < window.end
            )
        )
        escalated = await self._count(
            scope.where(
                Conversation.escalated_at.is_not(None),
                Conversation.escalated_at >= window.start,
                Conversation.escalated_at < window.end,
            )
        )
        open_now = await self._count(scope.where(Conversation.status == ConversationStatus.ACTIVE))
        return ConversationCounts(started=started, escalated=escalated, open_now=open_now)

    async def message_totals(
        self, window: Window, agent_id: uuid.UUID | None, include_preview: bool
    ) -> MessageTotals:
        """Counts, tokens and cost over the window.

        The quality signals (§5.8) are counted here rather than inferred later. A reply written when
        retrieval found nothing carries ``hasContext: false`` in its metadata, and a reply produced
        by a restricted-topic guardrail carries ``guardrail: declined`` — those two markers are what
        the "I don't know" rate is measured from. Matching on the *text* of an answer would be
        guesswork, and would break the moment a tenant reworded their fallback.
        """
        row = (
            await self.session.execute(
                self._messages_in(window, agent_id, include_preview).add_columns(
                    func.count(),
                    func.count().filter(Message.role == MessageRole.USER),
                    func.count().filter(Message.role == MessageRole.ASSISTANT),
                    func.coalesce(func.sum(Message.prompt_tokens), 0),
                    func.coalesce(func.sum(Message.completion_tokens), 0),
                    func.coalesce(func.sum(Message.cost_micro_usd), 0),
                    func.count().filter(Message.cost_micro_usd.is_not(None)),
                    func.count().filter(
                        Message.role == MessageRole.ASSISTANT,
                        Message.meta_json["hasContext"].astext == "false",
                    ),
                    func.count().filter(
                        Message.meta_json["guardrail"].astext == "declined",
                    ),
                )
            )
        ).one()

        return MessageTotals(
            total=int(row[0]),
            user=int(row[1]),
            assistant=int(row[2]),
            prompt_tokens=int(row[3]),
            completion_tokens=int(row[4]),
            cost_micro_usd=int(row[5]),
            priced_messages=int(row[6]),
            without_context=int(row[7]),
            declined=int(row[8]),
        )

    async def daily(
        self, window: Window, agent_id: uuid.UUID | None, include_preview: bool
    ) -> list[DailyPoint]:
        """Messages, tokens and cost per day — the "messages/day" chart §5.8 asks for.

        Days with no traffic are absent rather than zero-filled. Filling them is the caller's
        decision: a chart wants a point per day, an export usually does not, and inventing rows in
        the repository takes that choice away from both.
        """
        conversations = self._conversation_scope(agent_id, include_preview).subquery()
        day = func.date_trunc("day", Message.created_at).label("day")

        rows = await self.session.execute(
            select(
                day,
                func.count(),
                func.count(func.distinct(Message.conversation_id)),
                func.coalesce(func.sum(Message.prompt_tokens + Message.completion_tokens), 0),
                func.coalesce(func.sum(Message.cost_micro_usd), 0),
            )
            .select_from(Message)
            .join(conversations, Message.conversation_id == conversations.c.id)
            .where(Message.created_at >= window.start, Message.created_at < window.end)
            .group_by(day)
            .order_by(day)
        )
        return [
            DailyPoint(
                day=row[0].date(),
                messages=int(row[1]),
                conversations=int(row[2]),
                tokens=int(row[3]),
                cost_micro_usd=int(row[4]),
            )
            for row in rows
        ]

    async def by_channel(
        self, window: Window, agent_id: uuid.UUID | None, include_preview: bool
    ) -> list[ChannelPoint]:
        conversations = self._conversation_scope(agent_id, include_preview).subquery()

        rows = await self.session.execute(
            select(
                conversations.c.channel,
                func.count(func.distinct(conversations.c.id)),
                func.count(Message.id),
            )
            .select_from(conversations)
            .join(
                Message,
                (Message.conversation_id == conversations.c.id)
                & (Message.created_at >= window.start)
                & (Message.created_at < window.end),
                isouter=True,
            )
            .group_by(conversations.c.channel)
            .order_by(func.count(Message.id).desc())
        )
        return [
            ChannelPoint(channel=Channel(row[0]), conversations=int(row[1]), messages=int(row[2]))
            for row in rows
        ]

    async def by_model(
        self, window: Window, agent_id: uuid.UUID | None, include_preview: bool
    ) -> list[ModelPoint]:
        """Spend split by provider and model — the "estimated cost by provider" of §5.8.

        Only assistant messages carry a provider, so the grouping naturally excludes the customer's
        own turns rather than filing them under a null model.
        """
        conversations = self._conversation_scope(agent_id, include_preview).subquery()

        rows = await self.session.execute(
            select(
                Message.provider,
                Message.model,
                func.count(),
                func.coalesce(func.sum(Message.prompt_tokens), 0),
                func.coalesce(func.sum(Message.completion_tokens), 0),
                func.coalesce(func.sum(Message.cost_micro_usd), 0),
            )
            .select_from(Message)
            .join(conversations, Message.conversation_id == conversations.c.id)
            .where(
                Message.created_at >= window.start,
                Message.created_at < window.end,
                Message.provider.is_not(None),
            )
            .group_by(Message.provider, Message.model)
            .order_by(func.coalesce(func.sum(Message.cost_micro_usd), 0).desc())
        )
        return [
            ModelPoint(
                provider=row[0],
                model=row[1],
                messages=int(row[2]),
                prompt_tokens=int(row[3]),
                completion_tokens=int(row[4]),
                cost_micro_usd=int(row[5]),
            )
            for row in rows
        ]

    async def _count(self, query: Select[tuple[Conversation]]) -> int:
        total = await self.session.execute(select(func.count()).select_from(query.subquery()))
        return int(total.scalar_one())


class AgentUsageRepository(TenantScopedRepository[Agent]):
    """Agent names for the per-agent breakdown.

    A read repository rather than a call into ``AgentService`` for one reason: the breakdown needs
    the name of every agent that appeared in the window, and asking the agents module row by row
    would be a query per agent to render a single table.
    """

    model = Agent

    async def names(self) -> dict[uuid.UUID, str]:
        rows = await self.session.execute(
            self._base_query().with_only_columns(Agent.id, Agent.name)
        )
        return {row[0]: row[1] for row in rows}


class PlatformEventRepository(TenantScopedRepository[PlatformEvent]):
    """This module's own table — the only one it writes to."""

    model = PlatformEvent

    async def count_in(self, window: Window, category: EventCategory) -> int:
        query = self._base_query().where(
            PlatformEvent.category == category,
            PlatformEvent.created_at >= window.start,
            PlatformEvent.created_at < window.end,
        )
        total = await self.session.execute(select(func.count()).select_from(query.subquery()))
        return int(total.scalar_one())

    async def recent(
        self, window: Window, category: EventCategory, limit: int
    ) -> list[FailureItem]:
        rows = await self.session.execute(
            self._base_query()
            .where(
                PlatformEvent.category == category,
                PlatformEvent.created_at >= window.start,
                PlatformEvent.created_at < window.end,
            )
            .order_by(PlatformEvent.created_at.desc())
            .limit(limit)
        )
        return [
            FailureItem(
                occurred_at=event.created_at,
                kind=event.category.value,
                code=event.code,
                detail=event.detail,
                subject=str(event.meta_json.get("subject") or event.code),
                subject_id=event.id,
                agent_id=event.agent_id,
            )
            for event in rows.scalars()
        ]


class IngestionFailureRepository(TenantScopedRepository[KbSource]):
    """Failed ingestions, read from the sources that own the failure (spec §5.8).

    Dated by ``updated_at``: a source fails when extraction ran, not when it was uploaded, and a
    document uploaded in January that broke on a re-sync today belongs in today's report.
    """

    model = KbSource

    def _failed(self, window: Window) -> Select[tuple[KbSource]]:
        return self._base_query().where(
            KbSource.status == SourceStatus.FAILED,
            KbSource.updated_at >= window.start,
            KbSource.updated_at < window.end,
        )

    async def count_in(self, window: Window) -> int:
        total = await self.session.execute(
            select(func.count()).select_from(self._failed(window).subquery())
        )
        return int(total.scalar_one())

    async def recent(self, window: Window, limit: int) -> list[FailureItem]:
        rows = await self.session.execute(
            self._failed(window).order_by(KbSource.updated_at.desc()).limit(limit)
        )
        return [
            FailureItem(
                occurred_at=source.updated_at,
                kind="ingestion",
                code="INGESTION_FAILED",
                detail=(source.error_detail or "")[:500] or None,
                subject=source.name,
                subject_id=source.id,
            )
            for source in rows.scalars()
        ]


class ToolFailureRepository(TenantScopedRepository[AgentTool]):
    """Tool calls that did not succeed.

    ``tool_call`` is not tenant-scoped, so the join runs the other way from every other repository
    here: start from the scoped tools and join their calls, rather than start from calls and filter.
    That ordering is what makes a cross-tenant read impossible rather than merely absent.
    """

    model = AgentTool

    FAILED_OUTCOMES = (ToolOutcome.FAILED, ToolOutcome.TIMED_OUT, ToolOutcome.REFUSED)

    def _failed(self, window: Window) -> Select[tuple[ToolCallLog, AgentTool]]:
        tools = self._base_query().subquery()
        return (
            select(ToolCallLog, tools.c.name, tools.c.agent_id)
            .join(tools, ToolCallLog.tool_id == tools.c.id)
            .where(
                ToolCallLog.outcome.in_(self.FAILED_OUTCOMES),
                ToolCallLog.created_at >= window.start,
                ToolCallLog.created_at < window.end,
            )
        )

    async def count_in(self, window: Window) -> int:
        total = await self.session.execute(
            select(func.count()).select_from(self._failed(window).subquery())
        )
        return int(total.scalar_one())

    async def recent(self, window: Window, limit: int) -> list[FailureItem]:
        rows = await self.session.execute(
            self._failed(window).order_by(ToolCallLog.created_at.desc()).limit(limit)
        )
        return [
            FailureItem(
                occurred_at=call.created_at,
                kind="tool",
                code=call.outcome.value.upper(),
                detail=call.error_detail,
                subject=str(name),
                subject_id=call.id,
                agent_id=agent_id,
            )
            for call, name, agent_id in rows
        ]


class ChannelFailureRepository(TenantScopedRepository[WhatsAppMessage]):
    """WhatsApp messages that were never delivered (spec §5.5)."""

    model = WhatsAppMessage

    def _failed(self, window: Window) -> Select[tuple[WhatsAppMessage]]:
        return self._base_query().where(
            WhatsAppMessage.status == DeliveryStatus.FAILED,
            WhatsAppMessage.created_at >= window.start,
            WhatsAppMessage.created_at < window.end,
        )

    async def count_in(self, window: Window) -> int:
        total = await self.session.execute(
            select(func.count()).select_from(self._failed(window).subquery())
        )
        return int(total.scalar_one())

    async def recent(self, window: Window, limit: int) -> list[FailureItem]:
        rows = await self.session.execute(
            self._failed(window).order_by(WhatsAppMessage.created_at.desc()).limit(limit)
        )
        return [
            FailureItem(
                occurred_at=message.created_at,
                kind="whatsapp",
                code="WHATSAPP_DELIVERY_FAILED",
                detail=message.error_detail,
                # The contact id, not the message body: a failure report is read by whoever is
                # fixing the integration, and it should not become a second copy of the transcript.
                subject=message.wa_contact_id,
                subject_id=message.id,
                agent_id=message.agent_id,
            )
            for message in rows.scalars()
        ]


class WebhookHealthRepository(TenantScopedRepository[WebhookEndpoint]):
    """Endpoints currently failing, from the counters the channels module maintains.

    This is a *state* read rather than an event read: ``failure_count`` is the number of consecutive
    failures right now, reset on the next success. It answers "which of my endpoints is broken",
    which is the question a tenant actually has — the individual delivery failures are recorded as
    platform events.
    """

    model = WebhookEndpoint

    async def failing(self, limit: int) -> list[FailureItem]:
        rows = await self.session.execute(
            self._base_query()
            .where(
                WebhookEndpoint.failure_count > 0,
                WebhookEndpoint.status == WebhookStatus.ACTIVE,
            )
            .order_by(WebhookEndpoint.failure_count.desc())
            .limit(limit)
        )
        return [
            FailureItem(
                occurred_at=endpoint.last_delivery_at or endpoint.updated_at,
                kind="webhook",
                code=f"CONSECUTIVE_FAILURES_{endpoint.failure_count}",
                detail=endpoint.last_error,
                subject=endpoint.url,
                subject_id=endpoint.id,
                agent_id=endpoint.agent_id,
            )
            for endpoint in rows.scalars()
        ]
