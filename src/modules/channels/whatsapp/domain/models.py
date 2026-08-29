"""The WhatsApp message ledger (spec §5.5, §6).

One table, and it exists for two things that are easy to get wrong and impossible to bolt on later.

**Idempotency is a unique constraint, not a check.** WhatsApp redelivers a webhook whenever it does
not see a prompt ``200`` — and it will happily deliver the same ``wamid`` several times. Answering
twice is the failure §6 singles out. The defence is
``uq_whatsapp_message_connection_id_provider_message_id``: the inbound row is inserted *before* any
work is done, so a duplicate loses the insert and stops there. A set in memory or a Redis key would
work until there were two workers, or until one restarted; a constraint holds regardless of how
many processes race, which is the only version of this that is actually true.

**The 24-hour window is derived from this table, not tracked beside it.** A contact's window opens
at their last inbound message, and that timestamp is already here — a separate ``last_inbound_at``
column somewhere else would be a second copy of a fact this table already holds, free to drift out
of agreement with it. The cost is one indexed query per send, which is the right trade.

``direction`` keeps both sides in one table because they are the same conversation and the delivery
receipts Meta sends arrive keyed by the outbound message's own ``wamid`` — the status callback and
the message it describes belong in one row, not in two tables joined by a string.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base_model import TenantScopedModel, enum_column


class MessageDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class DeliveryStatus(str, enum.Enum):
    """Where a message got to.

    The inbound half uses ``RECEIVED`` → ``PROCESSED`` (or ``FAILED``): the webhook is acknowledged
    the moment the row lands, and the turn that follows runs on the queue, so the status is how a
    tenant sees that a message arrived but its answer did not.

    The outbound half mirrors WhatsApp's own receipts — ``SENT``, ``DELIVERED``, ``READ`` — because
    inventing our own vocabulary for statuses the provider already names would only mean translating
    twice. ``QUEUED`` is ours: the moment before the provider has accepted it.
    """

    QUEUED = "queued"
    RECEIVED = "received"
    PROCESSED = "processed"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class MessageType(str, enum.Enum):
    """What kind of content this was, in WhatsApp's terms.

    ``TEMPLATE`` is not a media type — it is how a message was *sent*, and it is recorded because
    "was this a template?" is the question asked when reconciling a bill or explaining why a
    contact received a stiff, pre-approved sentence instead of an answer.
    """

    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"
    VIDEO = "video"
    TEMPLATE = "template"
    UNSUPPORTED = "unsupported"


class WhatsAppMessage(TenantScopedModel):
    """One message in either direction, with what the provider said became of it."""

    __tablename__ = "whatsapp_message"
    __table_args__ = (
        # The idempotency guarantee. Scoped to the connection rather than global because a
        # provider's message ids are only unique within its own account, and two tenants must never
        # be able to collide — or to probe each other by guessing an id.
        UniqueConstraint(
            "connection_id",
            "provider_message_id",
            name="uq_whatsapp_message_connection_id_provider_message_id",
        ),
        # Serves the session-window lookup: the newest inbound message for one contact.
        Index(
            "ix_whatsapp_message_contact",
            "connection_id",
            "wa_contact_id",
            "direction",
            "created_at",
        ),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("channel_config.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="The channel_config row holding this number's credentials.",
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversation.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Null until the turn runs, and again if the conversation is later deleted.",
    )
    direction: Mapped[MessageDirection] = mapped_column(
        enum_column(MessageDirection, "whatsapp_message_direction")
    )
    # The provider's own id — `wamid.…` on Meta. Nullable because an outbound row is written before
    # the send, so a message the provider refused outright still leaves a record of the attempt.
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The contact's WhatsApp id: a phone number in E.164 without the '+'. Opaque here — it is the
    # session key the channel-agnostic format calls `external_user_id`, and nothing parses it.
    wa_contact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    message_type: Mapped[MessageType] = mapped_column(
        enum_column(MessageType, "whatsapp_message_type"),
        nullable=False,
        default=MessageType.TEXT,
        server_default=MessageType.TEXT.value,
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        enum_column(DeliveryStatus, "whatsapp_delivery_status"),
        nullable=False,
        default=DeliveryStatus.QUEUED,
        server_default=DeliveryStatus.QUEUED.value,
    )
    # What was said, as text. Media contributes the text extracted from it, so a transcript reads
    # the same whether the contact typed a question or photographed one.
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Media ids, the reply-to id, the raw status reason — the things worth keeping for support but
    # not worth a column each. Nothing queries inside it.
    meta_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
