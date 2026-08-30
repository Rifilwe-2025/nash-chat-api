"""Analytics: the read model over everything the other modules recorded (spec §5.8).

Two services, kept apart because they are used by different people at different times.

:class:`AnalyticsService` is what a signed-in tenant reads: usage, cost, quality signals, the
citation trace behind one conversation, and the failure report. It only ever reads. Nothing in it
writes to another module's tables, and nothing in it recomputes a number another module already
stored — the tokens on a message were measured by the provider (Phase 4) and the cost was priced
when the message was written, so a total here is a sum of stored facts rather than a fresh estimate.
That is what makes the dashboard reconcile with the transcript.

:class:`PlatformEventService` is what other modules call when something failed with nobody waiting
on it. It is the only writer in this module, and its whole job is one insert — see
:mod:`src.modules.analytics.domain.models` for why those particular failures need a table when the
others do not.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.agents.domain.services import AgentService
from src.modules.analytics.domain.models import EventCategory, PlatformEvent
from src.modules.analytics.domain.repositories import (
    AgentUsageRepository,
    ChannelFailureRepository,
    ChannelPoint,
    ConversationCounts,
    DailyPoint,
    FailureItem,
    IngestionFailureRepository,
    MessageTotals,
    ModelPoint,
    PlatformEventRepository,
    ToolFailureRepository,
    UsageRepository,
    WebhookHealthRepository,
)
from src.modules.analytics.internal.windows import Window, rate
from src.modules.conversations.domain.models import Message
from src.modules.conversations.domain.services import ConversationService
from src.shared.database.pagination import Page, PageRequest

logger = logging.getLogger("api.analytics")


@dataclass(frozen=True, slots=True)
class QualitySignals:
    """The two signals §5.8 asks for, plus the counts they were computed from.

    The counts are returned alongside the rates on purpose. "A 50% fallback rate" means something
    entirely different over four messages and over four thousand, and a dashboard that shows only
    the percentage invites the wrong conclusion on a quiet day.
    """

    answered: int
    without_context: int
    declined: int
    fallback_rate: float
    conversations: int
    escalated: int
    escalation_rate: float


@dataclass(frozen=True, slots=True)
class UsageReport:
    """The dashboard-shaped payload for a tenant or a single agent."""

    window: Window
    agent_id: uuid.UUID | None
    include_preview: bool
    conversations: ConversationCounts
    messages: MessageTotals
    quality: QualitySignals
    daily: list[DailyPoint]
    channels: list[ChannelPoint]
    models: list[ModelPoint]


@dataclass(frozen=True, slots=True)
class FailureClass:
    """One kind of failure: how many there were, and the most recent few."""

    kind: str
    count: int
    recent: list[FailureItem]


@dataclass(frozen=True, slots=True)
class FailureReport:
    window: Window
    classes: list[FailureClass]

    @property
    def total(self) -> int:
        return sum(item.count for item in self.classes)


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """One message, with everything that shaped it.

    This is the debugging view §5.8 calls the "conversation logs viewer with citation trace": what
    was asked, what was answered, which sources grounded it, which tools ran, and what it cost.
    """

    message: Message
    citations: list[Any]
    tier: str | None
    has_context: bool | None
    tool_calls: list[Any]
    guardrail: str | None


class AnalyticsService:
    """Read-only. Every repository it holds is tenant-scoped by construction."""

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.usage = UsageRepository(session, tenant_id)
        self.agent_names = AgentUsageRepository(session, tenant_id)
        self.events = PlatformEventRepository(session, tenant_id)
        self.ingestion_failures = IngestionFailureRepository(session, tenant_id)
        self.tool_failures = ToolFailureRepository(session, tenant_id)
        self.channel_failures = ChannelFailureRepository(session, tenant_id)
        self.webhook_health = WebhookHealthRepository(session, tenant_id)
        # Service to service: the agent must be checked through the module that owns it, so a
        # request for another tenant's agent is a 404 here for the same reason it is everywhere.
        self.agents = AgentService(session, tenant_id)
        self.conversations = ConversationService(session, tenant_id)

    # -- usage ---------------------------------------------------------------

    async def report(
        self,
        window: Window,
        agent_id: uuid.UUID | None = None,
        include_preview: bool = False,
    ) -> UsageReport:
        """Everything a usage dashboard needs, for the tenant or for one agent.

        Assembled in one call rather than one endpoint per number: a dashboard that fetched these
        separately would show figures from six different instants, and the totals would not add up
        while traffic was arriving.
        """
        if agent_id is not None:
            # Raises AGENT_NOT_FOUND for an agent in another tenant, which is what stops this
            # endpoint being a way to probe for the existence of one.
            await self.agents.get(agent_id)

        conversations = await self.usage.conversation_counts(window, agent_id, include_preview)
        messages = await self.usage.message_totals(window, agent_id, include_preview)

        return UsageReport(
            window=window,
            agent_id=agent_id,
            include_preview=include_preview,
            conversations=conversations,
            messages=messages,
            quality=QualitySignals(
                answered=messages.assistant,
                without_context=messages.without_context,
                declined=messages.declined,
                fallback_rate=rate(messages.without_context, messages.assistant),
                conversations=conversations.started,
                escalated=conversations.escalated,
                escalation_rate=rate(conversations.escalated, conversations.started),
            ),
            daily=await self.usage.daily(window, agent_id, include_preview),
            channels=await self.usage.by_channel(window, agent_id, include_preview),
            models=await self.usage.by_model(window, agent_id, include_preview),
        )

    async def agent_names_by_id(self) -> dict[uuid.UUID, str]:
        return await self.agent_names.names()

    # -- conversation trace --------------------------------------------------

    async def trace(self, conversation_id: uuid.UUID, page: PageRequest) -> Page[TraceEntry]:
        """One conversation, annotated with what produced each answer.

        The conversation is loaded through ``ConversationService``, not through a query here: it is
        that module's row, its 404 and its tenant check, and duplicating them would be one more
        place for the isolation rule to be got wrong.

        Everything below the message itself is read out of ``meta_json`` and ``citations_json``,
        which the conversation engine wrote at the time (Phases 6 and 7). Nothing is recomputed — a
        trace that re-derived which sources *would* be retrieved today would answer a different
        question from the one being asked, which is always "why did it say that?".
        """
        transcript = await self.conversations.transcript(conversation_id, page)

        return Page(
            items=[
                TraceEntry(
                    message=message,
                    citations=list(message.citations_json or []),
                    tier=_string(message.meta_json.get("tier")),
                    has_context=_boolean(message.meta_json.get("hasContext")),
                    tool_calls=list(message.meta_json.get("toolCalls") or []),
                    guardrail=_string(message.meta_json.get("guardrail")),
                )
                for message in transcript.items
            ],
            total=transcript.total,
            page=transcript.page,
            page_size=transcript.page_size,
        )

    # -- failures ------------------------------------------------------------

    async def failures(self, window: Window, recent_limit: int) -> FailureReport:
        """Every failure class §5.8 names, each read from wherever it is actually recorded.

        The counts and the samples come from five different tables, so they are gathered here rather
        than in one query: a union across tables with nothing in common but a timestamp would need
        every column cast to a lowest common denominator, and the result would be slower and less
        honest than five small indexed reads.

        ``webhook`` is the odd one out and is documented as such in the response: it reports
        endpoints that are *currently* failing rather than deliveries that failed in the window,
        because a consecutive-failure counter is what the channels module keeps. Its count is
        therefore bounded by ``recent_limit``, which the others are not.
        """
        failing_webhooks = await self.webhook_health.failing(recent_limit)

        return FailureReport(
            window=window,
            classes=[
                FailureClass(
                    kind="ingestion",
                    count=await self.ingestion_failures.count_in(window),
                    recent=await self.ingestion_failures.recent(window, recent_limit),
                ),
                FailureClass(
                    kind="provider",
                    count=await self.events.count_in(window, EventCategory.PROVIDER_ERROR),
                    recent=await self.events.recent(
                        window, EventCategory.PROVIDER_ERROR, recent_limit
                    ),
                ),
                FailureClass(
                    # State, not events: `failure_count` is how many times in a row this endpoint
                    # has failed *right now*, reset by the next success. It is capped at
                    # `recent_limit` like the others, so a tenant with a hundred broken endpoints
                    # sees the worst few rather than all of them.
                    kind="webhook",
                    count=len(failing_webhooks),
                    recent=failing_webhooks,
                ),
                FailureClass(
                    kind="channel",
                    count=await self.channel_failures.count_in(window),
                    recent=await self.channel_failures.recent(window, recent_limit),
                ),
                FailureClass(
                    kind="tool",
                    count=await self.tool_failures.count_in(window),
                    recent=await self.tool_failures.recent(window, recent_limit),
                ),
            ],
        )


class PlatformEventService:
    """Records a failure nobody is waiting on. The only writer in this module.

    Kept separate from :class:`AnalyticsService` so the modules that call it — conversations,
    channels — depend on an insert and nothing else. Importing the read service would pull the
    whole read model, and with it a dependency on every module analytics reads.

    **Recording never raises.** This is called from paths that are already failing; a log write that
    threw would turn one failure into two, and the second would be ours.
    """

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.events = PlatformEventRepository(session, tenant_id)

    async def record(
        self,
        category: EventCategory,
        code: str,
        detail: str | None = None,
        agent_id: uuid.UUID | None = None,
        meta: dict[str, Any] | None = None,
    ) -> PlatformEvent | None:
        try:
            return await self.events.add(
                PlatformEvent(
                    agent_id=agent_id,
                    category=category,
                    code=code[:64],
                    detail=(detail or "")[:500] or None,
                    meta_json=meta or {},
                )
            )
        except Exception:
            logger.exception("could not record a %s platform event", category.value)
            return None


def _string(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None
