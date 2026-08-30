"""Analytics response shapes (spec §5.8).

Two conventions run through all of them.

**Money is micro-USD, and it is nullable in spirit.** Costs are summed in millionths of a dollar
because that is how they are stored — a float would accumulate error over a month of messages, and
rounding to cents would lose every individual figure. ``pricedMessages`` sits beside every cost so a
reader can distinguish "this was free" from "no price is configured for that model", which is a
distinction the platform makes deliberately (``shared/llm/pricing.py``).

**Every payload carries its window.** A number without the period it covers is not a number anyone
can act on, and echoing the resolved window back is also how a caller confirms that the dates they
sent were understood.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import Field

from src.modules.conversations.domain.models import Channel, MessageRole
from src.shared.responses import CamelModel


class WindowResponse(CamelModel):
    """The half-open ``[start, end)`` period every figure in the payload was counted over."""

    start: datetime = Field(description="Inclusive start of the window, in UTC.")
    end: datetime = Field(description="Exclusive end of the window, in UTC.")
    days: int = Field(description="Whole days the window spans.", examples=[30])


class ConversationCountsResponse(CamelModel):
    started: int = Field(description="Conversations that began inside the window.")
    escalated: int = Field(description="Conversations handed to a human inside the window.")
    open_now: int = Field(
        description=(
            "Conversations still active **right now**, not inside the window — an open "
            "conversation is a fact about this moment, so dating it would make it meaningless."
        )
    )


class MessageTotalsResponse(CamelModel):
    total: int = Field(description="Every message stored in the window, both sides.")
    user: int = Field(description="Messages sent by customers.")
    assistant: int = Field(description="Replies produced by the agent.")
    prompt_tokens: int = Field(description="Input tokens, as reported by the provider.")
    completion_tokens: int = Field(description="Output tokens, as reported by the provider.")
    total_tokens: int = Field(description="Prompt plus completion.")
    cost_micro_usd: int = Field(
        description=(
            "Estimated spend in millionths of a US dollar, summed from the price recorded on each "
            "message. Divide by 1,000,000 for dollars."
        ),
        examples=[1_234_500],
    )
    priced_messages: int = Field(
        description=(
            "How many of the replies had a configured price. When this is below `assistant`, the "
            "cost is a floor rather than a total — the rest used models with no price set."
        )
    )


class QualityResponse(CamelModel):
    """The two quality signals of §5.8, with the counts behind them."""

    answered: int = Field(description="Replies the agent produced in the window.")
    without_context: int = Field(
        description=(
            "Replies written when retrieval found nothing relevant — the agent's \"I don't know\" "
            "case. Measured from the marker the engine stored at the time, not by matching the "
            "text of the answer."
        )
    )
    declined: int = Field(
        description="Replies produced by a restricted-topic guardrail rather than by the model."
    )
    fallback_rate: float = Field(
        description="`withoutContext ÷ answered`, from 0 to 1.", examples=[0.12]
    )
    conversations: int = Field(description="Conversations started in the window.")
    escalated: int = Field(description="Of those, how many reached a human.")
    escalation_rate: float = Field(
        description="`escalated ÷ conversations`, from 0 to 1.", examples=[0.04]
    )


class DailyPointResponse(CamelModel):
    """One day of traffic. Days with nothing on them are omitted rather than sent as zeroes."""

    day: date
    messages: int
    conversations: int = Field(description="Distinct conversations that saw a message that day.")
    tokens: int
    cost_micro_usd: int


class ChannelPointResponse(CamelModel):
    channel: Channel
    conversations: int
    messages: int


class ModelPointResponse(CamelModel):
    """Spend for one provider and model — what §5.8 calls the cost estimate by provider."""

    provider: str | None
    model: str | None
    messages: int
    prompt_tokens: int
    completion_tokens: int
    cost_micro_usd: int


class UsageReportResponse(CamelModel):
    """The dashboard payload, for the whole tenant or for one agent."""

    window: WindowResponse
    agent_id: uuid.UUID | None = Field(
        default=None, description="Present when the report covers a single agent."
    )
    includes_preview: bool = Field(
        description=(
            "Whether the builder's test chat was counted. False by default: previews are the "
            "tenant trying their own agent out, and counting them as customer traffic would "
            "inflate every figure here."
        )
    )
    conversations: ConversationCountsResponse
    messages: MessageTotalsResponse
    quality: QualityResponse
    daily: list[DailyPointResponse]
    channels: list[ChannelPointResponse]
    models: list[ModelPointResponse]


class FailureItemResponse(CamelModel):
    occurred_at: datetime
    kind: str = Field(description="Which failure class this came from.", examples=["ingestion"])
    code: str = Field(description="Stable, machine-readable reason.", examples=["INGESTION_FAILED"])
    detail: str | None = Field(default=None, description="What went wrong, in prose.")
    subject: str = Field(
        description="What failed — a source name, a tool name, a contact, an endpoint URL.",
        examples=["refund-policy.pdf"],
    )
    subject_id: uuid.UUID = Field(description="Identifier of the row this came from.")
    agent_id: uuid.UUID | None = None


class FailureClassResponse(CamelModel):
    kind: str = Field(examples=["ingestion", "provider", "webhook", "channel", "tool"])
    count: int
    recent: list[FailureItemResponse]


class FailureReportResponse(CamelModel):
    window: WindowResponse
    total: int = Field(description="Sum of every class's count.")
    classes: list[FailureClassResponse]


class TraceEntryResponse(CamelModel):
    """One message in the debugging view, with what shaped it."""

    id: uuid.UUID
    sequence: int
    role: MessageRole
    content: str
    created_at: datetime
    provider: str | None = None
    model: str | None = None
    prompt_tokens: int
    completion_tokens: int
    cost_micro_usd: int | None = None
    citations: list[dict[str, Any]] = Field(
        default_factory=list,
        description="The sources this answer was grounded in, as recorded when it was written.",
    )
    tier: str | None = Field(
        default=None,
        description="Which retrieval tier ran for this turn: `direct` or `keyword`.",
        examples=["keyword"],
    )
    has_context: bool | None = Field(
        default=None,
        description="Whether retrieval found anything relevant. `false` is the fallback case.",
    )
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list, description="Tools the model ran during this turn, with outcomes."
    )
    guardrail: str | None = Field(
        default=None,
        description="Set when the reply came from a guardrail: `escalated` or `declined`.",
    )


class OperationsResponse(CamelModel):
    """Process-level telemetry for whoever operates the deployment, not for a tenant.

    Counters live in memory and reset when the process restarts, so they describe *this* worker
    rather than the platform. Anything a tenant needs to see later is in a table instead.
    """

    uptime_seconds: float
    counters: list[dict[str, Any]]
    timings: list[dict[str, Any]]
    series_dropped: int = Field(
        description=(
            "Series refused because the cardinality cap was reached. Anything above zero means "
            "some metrics are being dropped and a label is too varied."
        )
    )
