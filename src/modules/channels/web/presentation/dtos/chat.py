"""Public chat API shapes (spec §5.5).

Deliberately the smallest surface that works. This is the contract a tenant's developer codes
against, and every field added here is one that can never be removed — internal detail like token
counts, retrieval tiers and costs stays in the tenant console, which is authenticated differently
and versioned differently.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from src.modules.conversations.domain.models import ConversationStatus, MessageRole
from src.shared.responses import CamelModel


class SendChatMessageRequest(CamelModel):
    message: str = Field(
        min_length=1,
        max_length=8000,
        description="What the user typed.",
        examples=["Do you deliver to Bulawayo?"],
    )
    user_id: str = Field(
        min_length=1,
        max_length=255,
        description=(
            "Stable identifier for the person talking — your own user id, or a random id you keep "
            "in the browser for anonymous visitors. Everything sent under one value continues the "
            "same conversation."
        ),
        examples=["visitor-8f21c3"],
    )


class ChatReplyResponse(CamelModel):
    conversation_id: uuid.UUID = Field(
        description="The conversation this turn belongs to. Stable across the session."
    )
    reply: str = Field(description="What the agent said.")
    escalated: bool = Field(
        description=(
            "True when a guardrail handed this conversation to a human. The agent will not answer "
            "further messages in it."
        )
    )


class ChatMessageResponse(CamelModel):
    id: uuid.UUID
    role: MessageRole = Field(description="Who spoke.", examples=["assistant"])
    content: str
    created_at: datetime


class ChatSessionResponse(CamelModel):
    conversation_id: uuid.UUID | None = Field(
        default=None, description="Absent when this user has no open conversation."
    )
    status: ConversationStatus | None = None
