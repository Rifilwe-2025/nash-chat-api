"""Conversations and the messages in them (spec §5.4, §7).

A conversation is keyed by **(agent, channel, external user)** — the same person talking to the same
agent on WhatsApp and on a website is two conversations, because the channels have different
identity, different history, and different delivery rules. ``channel`` exists from this phase even
though only the builder preview writes to it, so the transports added in Phases 8 and 10 slot into
a shape that already accounts for them (§5.5's channel-agnostic message format).

There is deliberately **no unique constraint** on that key. A conversation ends — closed, or handed
to a human — and the same person comes back tomorrow; that is a new conversation with fresh history,
not a violation. The service resolves the *open* session for a key, which is what "session" means
here.

``summary`` is what makes a long conversation affordable. Once history outgrows the model's budget
the oldest turns are folded into a rolling summary on the conversation and dropped from the prompt,
so cost stays bounded no matter how long someone talks (§5.4).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.database.base_model import BaseModel, TenantScopedModel, enum_column


class Channel(str, enum.Enum):
    """Where a conversation is happening.

    ``PREVIEW`` is the builder's test chat (§5.1, journey step 3) and is kept distinct from real
    traffic on purpose: a tenant trying out their agent should not pollute their own analytics.
    """

    PREVIEW = "preview"
    WEB = "web"
    WHATSAPP = "whatsapp"


class ConversationStatus(str, enum.Enum):
    ACTIVE = "active"
    ESCALATED = "escalated"
    CLOSED = "closed"


class MessageRole(str, enum.Enum):
    """``SUMMARY`` is not a turn anyone spoke.

    It records what a stretch of dropped history was folded into, so the transcript stays readable
    after trimming and a support engineer can see what the model was actually told.
    """

    USER = "user"
    ASSISTANT = "assistant"
    SUMMARY = "summary"


class Conversation(TenantScopedModel):
    __tablename__ = "conversation"
    __table_args__ = (
        Index(
            "ix_conversation_session",
            "agent_id",
            "channel",
            "external_user_id",
            "status",
        ),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel: Mapped[Channel] = mapped_column(enum_column(Channel, "conversation_channel"))
    # Opaque and channel-defined: a phone number on WhatsApp, a browser session id on the web.
    # Never parsed here — the whole point of the channel-agnostic format (§5.5).
    external_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[ConversationStatus] = mapped_column(
        enum_column(ConversationStatus, "conversation_status"),
        nullable=False,
        default=ConversationStatus.ACTIVE,
        server_default=ConversationStatus.ACTIVE.value,
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Rolling summary of turns that have been trimmed out of the prompt.",
    )
    # How many messages the summary already covers, so summarisation resumes where it left off
    # rather than re-reading the whole transcript every time.
    summarised_through: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    meta_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.sequence",
    )


class Message(BaseModel):
    """One turn, plus what it cost.

    Token counts come from the provider's own accounting via Phase 4's ``TokenUsage``, so they are
    measured rather than estimated. ``cost_micro_usd`` is populated only where a price is configured
    for the model — the platform does not guess at pricing it has not been told (see
    ``src/shared/llm/pricing.py``).

    Not tenant-scoped: a message is only ever reached through its conversation, which is.
    """

    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_message_conversation_id_sequence"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversation.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Ordering within a conversation, and the reason it is not `created_at`: Postgres `now()` is
    # *transaction* time, so the two messages of a single turn share a timestamp exactly and would
    # then sort by random UUID. A transcript that shuffles the question and its answer is not a
    # cosmetic problem — history pairing and trimming both depend on the order being real.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[MessageRole] = mapped_column(enum_column(MessageRole, "message_role"))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Micro-USD (millionths) rather than a float: money in floating point accumulates error, and
    # per-message costs are small enough that rounding to cents would lose all of it.
    cost_micro_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Which sources the answer was grounded in, for the citation trail §5.8 asks for.
    citations_json: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    meta_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens
