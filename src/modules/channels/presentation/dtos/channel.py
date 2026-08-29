"""Channel configuration, webhook and integration-doc shapes (spec §5.5, §5.6)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from src.modules.channels.domain.models import (
    ChannelStatus,
    ChannelType,
    WebhookEvent,
    WebhookStatus,
)
from src.shared.responses import CamelModel


class CreateWebhookRequest(CamelModel):
    url: str = Field(
        min_length=1,
        max_length=2000,
        description="Where deliveries are POSTed. Must be reachable from the internet.",
        examples=["https://example.com/hooks/nash"],
    )
    events: list[WebhookEvent] = Field(
        min_length=1,
        description="Which events to receive.",
        examples=[["conversation.started", "conversation.escalated"]],
    )
    agent_id: uuid.UUID | None = Field(
        default=None,
        description="Restrict to one agent. Omit to receive events for every agent you own.",
    )


class UpdateWebhookRequest(CamelModel):
    """Every field is optional — omitted fields are left unchanged."""

    url: str | None = Field(default=None, min_length=1, max_length=2000)
    events: list[WebhookEvent] | None = None
    status: WebhookStatus | None = Field(
        default=None, description="Set `disabled` to stop deliveries without deleting the endpoint."
    )


class WebhookResponse(CamelModel):
    id: uuid.UUID
    agent_id: uuid.UUID | None = None
    url: str
    events: list[str]
    status: WebhookStatus
    secret: str = Field(
        description=(
            "Signing secret. Verify every delivery's signature against it — a webhook URL is not a "
            "secret, and anyone who guesses yours can post to it."
        ),
        examples=["whsec_9dK0gH5jL…"],
    )
    failure_count: int = Field(description="Consecutive failed deliveries. Resets on success.")
    last_delivery_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime


class WebhookTestResponse(CamelModel):
    delivered: bool
    error: str | None = Field(
        default=None, description="Why the test delivery failed. Absent on success."
    )


class ConfigureChannelRequest(CamelModel):
    settings: dict[str, Any] | None = Field(
        default=None,
        description="Channel settings. For `web`, the origins allowed to embed the widget.",
        examples=[{"allowedOrigins": ["https://example.com"]}],
    )
    credentials: dict[str, Any] | None = Field(
        default=None,
        description="Channel credentials. Unused by `web`; WhatsApp needs them from Phase 10.",
    )


class ChannelConfigResponse(CamelModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    channel_type: ChannelType
    status: ChannelStatus
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class IntegrationDocsResponse(CamelModel):
    """Documentation for one agent, generated from the live API schema."""

    agent_id: uuid.UUID
    agent_name: str
    base_url: str
    markdown: str = Field(
        description=(
            "The integration guide, in Markdown. Generated from the schema this API is currently "
            "serving, so it cannot describe a route that no longer exists."
        )
    )
