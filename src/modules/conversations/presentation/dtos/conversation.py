"""Conversation request and response shapes (spec §5.4).

The turn response carries more than the reply text on purpose: which tier answered, which sources
grounded it, and what it cost. That is the material a tenant needs while tuning an agent in the
builder, and it is the same trail §5.8 asks for in the conversation log viewer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from src.modules.conversations.domain.models import (
    Channel,
    ConversationStatus,
    MessageRole,
)
from src.modules.knowledge_base.domain.models import RetrievalTier
from src.shared.responses import CamelModel


class SendMessageRequest(CamelModel):
    agent_id: uuid.UUID = Field(description="The agent to talk to.")
    message: str = Field(
        min_length=1,
        max_length=8000,
        description="What the end user said.",
        examples=["Can I return tinted paint?"],
    )
    conversation_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Continue this specific conversation. Omit it and the open session for your "
            "`externalUserId` is continued, or a new one started."
        ),
    )
    external_user_id: str | None = Field(
        default=None,
        max_length=255,
        description=(
            "Who is talking, in whatever terms the channel uses. Sessions are keyed by agent, "
            "channel and this value. Defaults to a shared preview session."
        ),
        examples=["preview-ada"],
    )


class EscalateRequest(CamelModel):
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="Why the conversation is being handed to a human. Recorded on the record.",
        examples=["Customer asked for a manager."],
    )


class CitationResponse(CamelModel):
    source_id: uuid.UUID
    kb_id: uuid.UUID
    source_name: str = Field(examples=["Returns policy.docx"])


class MessageResponse(CamelModel):
    id: uuid.UUID
    role: MessageRole = Field(
        description=(
            "`user` and `assistant` are spoken turns. `summary` is not something anyone said — it "
            "records what a stretch of trimmed history was folded into."
        ),
        examples=["assistant"],
    )
    content: str
    provider: str | None = Field(default=None, examples=["gemini"])
    model: str | None = Field(default=None, examples=["gemini-2.0-flash"])
    prompt_tokens: int = Field(description="Measured by the provider, not estimated.")
    completion_tokens: int
    cost_micro_usd: int | None = Field(
        default=None,
        description=(
            "Cost in millionths of a dollar. Absent when no price is configured for the model — "
            "the platform records tokens it measured, and never guesses at pricing."
        ),
        examples=[1850],
    )
    citations: list[CitationResponse] = Field(
        default_factory=list, description="Sources the answer was grounded in."
    )
    created_at: datetime


class TurnResponse(CamelModel):
    """One completed exchange, with the reasoning behind it."""

    conversation_id: uuid.UUID
    status: ConversationStatus = Field(
        description="`escalated` means a guardrail handed the conversation to a human.",
        examples=["active"],
    )
    reply: MessageResponse
    escalated: bool = Field(
        description="True when this turn triggered a handoff. The agent stops answering after it."
    )
    retrieval_tier: RetrievalTier | None = Field(
        default=None,
        description="Which tier answered. Absent when a guardrail replied without retrieval.",
    )
    used_knowledge: bool = Field(
        description=(
            "False when nothing relevant was found. The agent was told so explicitly and, if it "
            "requires grounded answers, will have said it cannot help rather than guessed."
        )
    )


class ConversationResponse(CamelModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    channel: Channel = Field(
        description="`preview` is the builder's test chat and is kept apart from real traffic.",
        examples=["preview"],
    )
    external_user_id: str
    status: ConversationStatus
    summary: str | None = Field(
        default=None, description="Rolling summary of turns trimmed out of the prompt."
    )
    escalated_at: datetime | None = None
    escalation_reason: str | None = None
    last_message_at: datetime | None = None
    created_at: datetime


class ConversationSummaryResponse(CamelModel):
    """Trimmed shape for list endpoints."""

    id: uuid.UUID
    agent_id: uuid.UUID
    channel: Channel
    external_user_id: str
    status: ConversationStatus
    last_message_at: datetime | None = None


class ConversationUsageResponse(CamelModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_micro_usd: int = Field(
        description="Total across messages that had a configured price; others contribute zero."
    )


class ConversationDetailResponse(CamelModel):
    conversation: ConversationResponse
    usage: ConversationUsageResponse


def citations_of(raw: list[Any]) -> list[CitationResponse]:
    """Stored citations are plain JSON.

    Anything malformed is skipped rather than breaking the read: a transcript is worth showing even
    if one citation from an older schema no longer parses.
    """
    parsed: list[CitationResponse] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            parsed.append(
                CitationResponse(
                    source_id=uuid.UUID(str(item["sourceId"])),
                    kb_id=uuid.UUID(str(item["kbId"])),
                    source_name=str(item["sourceName"]),
                )
            )
        except (KeyError, ValueError):
            continue
    return parsed
