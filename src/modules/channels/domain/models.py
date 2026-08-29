"""Channel configuration and outbound webhooks (spec §5.5, §5.6, §7).

``channel_config`` holds the per-agent settings a channel needs — allowed origins for the web
widget now, WhatsApp's phone number id and tokens in Phase 10. It exists in this phase, with only
the web channel using it, so the WhatsApp adapter has somewhere to live that is not a new table.

``credentials_json`` is where a channel's secrets go. They are stored, not hashed, because the
platform has to *present* them to the channel — the same reason a webhook signing secret is stored
in clear. Encryption at rest is a §5.7 concern for hardening (Phase 13), noted rather than faked
here: pretending a JSON column is secure would be worse than being plain about what it is.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base_model import TenantScopedModel, enum_column


class ChannelType(str, enum.Enum):
    WEB = "web"
    WHATSAPP = "whatsapp"


class ChannelStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class WebhookEvent(str, enum.Enum):
    """Platform events a tenant can subscribe to (spec §5.6).

    Both are moments a human may need to act on: a conversation starting is a lead, and an
    escalation is someone waiting. Message-level events are deliberately absent — a webhook per
    message is a firehose, and the transcript endpoint already serves that need.
    """

    CONVERSATION_STARTED = "conversation.started"
    CONVERSATION_ESCALATED = "conversation.escalated"


class WebhookStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ChannelConfig(TenantScopedModel):
    __tablename__ = "channel_config"
    __table_args__ = (
        UniqueConstraint(
            "agent_id", "channel_type", name="uq_channel_config_agent_id_channel_type"
        ),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_type: Mapped[ChannelType] = mapped_column(enum_column(ChannelType, "channel_type"))
    status: Mapped[ChannelStatus] = mapped_column(
        enum_column(ChannelStatus, "channel_status"),
        nullable=False,
        default=ChannelStatus.ACTIVE,
        server_default=ChannelStatus.ACTIVE.value,
    )
    credentials_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    settings_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )


class WebhookEndpoint(TenantScopedModel):
    """Where platform events are delivered.

    ``secret`` is stored in clear, unlike an API key, and the difference is the direction of trust:
    an API key is a credential *we* verify, so a hash suffices; a webhook secret is one the
    *receiver* verifies our signature with, so we must still be able to compute that signature.
    """

    __tablename__ = "webhook_endpoint"

    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        doc="Restrict deliveries to one agent. Null means every agent in the tenant.",
    )
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    events: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[WebhookStatus] = mapped_column(
        enum_column(WebhookStatus, "webhook_status"),
        nullable=False,
        default=WebhookStatus.ACTIVE,
        server_default=WebhookStatus.ACTIVE.value,
    )
    # Consecutive failures, for the alerting §5.2.1 asks for. Reset on any success.
    failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_delivery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    def subscribes_to(self, event: WebhookEvent) -> bool:
        return event.value in (self.events or [])
