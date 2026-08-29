"""WhatsApp connection, send and delivery-log shapes (spec §5.5).

Note what is *absent* from every response: the access token, the app secret, and the verify token.
They go in and never come back — a response carries ``hasAccessToken: true`` instead, which answers
"is my connection complete?" without handing a credential back over the wire (§5.7). The verify
token is the one exception, and only on the connect response, because a tenant has to paste it into
Meta's form and this is the single moment they need to see it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from src.modules.channels.domain.models import ChannelStatus
from src.modules.channels.whatsapp.domain.models import (
    DeliveryStatus,
    MessageDirection,
    MessageType,
)
from src.shared.responses import CamelModel

# The kinds WhatsApp will render as media. `text` and `unsupported` are inbound-only concepts,
# so the outbound surface names the four that are actually sendable rather than reusing an enum
# with two members a caller must be told not to pick.
MediaKind = Literal["image", "document", "audio", "video"]


class WhatsAppTemplateRequest(CamelModel):
    """A pre-approved WhatsApp template.

    Templates are approved by Meta, not by us: the name and language must already exist in the
    tenant's WhatsApp Manager, and a name that does not is rejected by the provider at send time.
    """

    name: str = Field(
        min_length=1,
        max_length=255,
        description="The template's name exactly as approved in WhatsApp Manager.",
        examples=["appointment_reminder"],
    )
    language: str = Field(
        default="en_US",
        max_length=16,
        description="The template's language code.",
        examples=["en_US", "en_GB"],
    )
    variables: list[str] = Field(
        default_factory=list,
        description=("Values for the template body's `{{1}}`, `{{2}}` … placeholders, in order."),
        examples=[["Tariro", "Thursday at 10am"]],
    )


class ConnectWhatsAppRequest(CamelModel):
    """Credentials from the tenant's own Meta app, plus how the agent should behave on the number.

    Every field but `phoneNumberId`, `accessToken` and `appSecret` is optional, and an omitted
    credential is left as it was — rotating a token does not mean re-pasting the app secret.
    """

    provider: str = Field(
        default="meta",
        description="Which WhatsApp provider these credentials are for.",
        examples=["meta"],
    )
    phone_number_id: str | None = Field(
        default=None,
        max_length=64,
        description="The phone number id from Meta's WhatsApp > API Setup page.",
        examples=["109876543210987"],
    )
    access_token: str | None = Field(
        default=None,
        description=(
            "A permanent system-user access token with `whatsapp_business_messaging`. Stored, "
            "never returned."
            "returned."
        ),
    )
    app_secret: str | None = Field(
        default=None,
        description=(
            "Your Meta app secret. Every inbound webhook is verified against it — without it, "
            "deliveries are refused rather than trusted."
        ),
    )
    business_account_id: str | None = Field(
        default=None, max_length=64, description="WhatsApp Business Account id, for your reference."
    )
    display_phone_number: str | None = Field(
        default=None,
        max_length=32,
        description="The number as customers see it. Shown back to you; never sent anywhere.",
        examples=["+263 77 000 0000"],
    )
    auto_reply: bool = Field(
        default=True,
        description=(
            "Whether the agent answers inbound messages. Off records them without replying, for a "
            "number your own team is staffing."
        ),
    )
    mark_read: bool = Field(
        default=True, description="Whether inbound messages are marked as read on WhatsApp."
    )
    outside_window_template: WhatsAppTemplateRequest | None = Field(
        default=None,
        description=(
            "The approved template used when a contact's 24-hour session window has closed. "
            "Without one, sends outside the window are refused rather than substituted."
        ),
    )
    status: ChannelStatus = Field(
        default=ChannelStatus.ACTIVE,
        description=(
            "Set `disabled` to pause the number without deleting its credentials. Inbound "
            "deliveries are then refused and nothing is answered."
        ),
    )


class WhatsAppConnectionResponse(CamelModel):
    """A connected number, and everything needed to finish the setup in Meta."""

    id: uuid.UUID = Field(description="Connection id. It appears in your webhook URL.")
    agent_id: uuid.UUID
    status: ChannelStatus
    credentials: dict[str, Any] = Field(
        description=(
            "Non-secret connection details, plus `hasAccessToken` / `hasAppSecret` flags. Secrets "
            "are never returned."
        ),
        examples=[{"provider": "meta", "phoneNumberId": "109…", "hasAccessToken": True}],
    )
    settings: dict[str, Any] = Field(description="Auto-reply, read receipts, and the template.")
    webhook_url: str = Field(
        description="Paste this into your Meta app's WhatsApp > Configuration > Callback URL.",
        examples=["https://api.example.com/v1/channels/whatsapp/webhook/6f1c…"],
    )
    verify_token: str | None = Field(
        default=None,
        description=(
            "Paste this into Meta's *Verify token* field. Returned only when the connection is "
            "created or updated — it is not readable afterwards."
        ),
    )
    created_at: datetime
    updated_at: datetime


class WhatsAppMediaRequest(CamelModel):
    """A file to send, by URL. WhatsApp fetches it itself, so the URL must be publicly reachable."""

    url: str = Field(
        min_length=1,
        max_length=2000,
        description="A public HTTPS URL WhatsApp can download the file from.",
        examples=["https://example.com/catalogue/matt-range.pdf"],
    )
    kind: MediaKind = Field(
        description="What kind of file this is. WhatsApp renders each differently.",
        examples=["document"],
    )
    caption: str | None = Field(
        default=None,
        max_length=1024,
        description="Text shown with the file. Ignored for `audio`, which WhatsApp captions never.",
    )


class SendWhatsAppRequest(CamelModel):
    """Send a message to one contact. Provide `text`, `media`, or a `template`."""

    to: str = Field(
        min_length=5,
        max_length=64,
        description="The contact's number in E.164 without the leading '+'.",
        examples=["263770000000"],
    )
    text: str | None = Field(
        default=None,
        max_length=4096,
        description=(
            "Free-form message. Delivered only inside the contact's 24-hour window; outside it, "
            "your configured template is sent instead."
        ),
        examples=["Your order is ready for collection."],
    )
    media: WhatsAppMediaRequest | None = Field(
        default=None,
        description=(
            "Send a file. Like `text`, it is free-form as far as WhatsApp is concerned, so it "
            "obeys the same 24-hour window rule."
        ),
    )
    template: WhatsAppTemplateRequest | None = Field(
        default=None,
        description=(
            "Send this template instead of free-form text. Always deliverable, window or not."
        ),
    )


class WhatsAppMessageResponse(CamelModel):
    """One message in the delivery log."""

    id: uuid.UUID
    direction: MessageDirection
    status: DeliveryStatus = Field(
        description=(
            "Inbound: `received` then `processed`. Outbound: `queued`, `sent`, `delivered`, "
            "`read` — or `failed`, with `errorDetail` saying why."
        )
    )
    message_type: MessageType
    contact_id: str = Field(description="The contact's WhatsApp id.", examples=["263770000000"])
    body: str | None = Field(default=None, description="The message text, where there was one.")
    template_name: str | None = None
    provider_message_id: str | None = Field(
        default=None, description="WhatsApp's own id for this message.", examples=["wamid.HBgM…"]
    )
    conversation_id: uuid.UUID | None = Field(
        default=None, description="The conversation this message belongs to, once one exists."
    )
    error_detail: str | None = None
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    read_at: datetime | None = None
    created_at: datetime


class SessionWindowResponse(CamelModel):
    """Whether free-form text can reach a contact right now (spec §5.5)."""

    contact_id: str
    is_open: bool = Field(
        description="True while the contact's 24-hour customer service window is open."
    )
    last_inbound_at: datetime | None = Field(
        default=None, description="When the contact last messaged. Null if they never have."
    )
    expires_at: datetime | None = Field(
        default=None, description="When the window closes. Null if it was never open."
    )
    seconds_remaining: int = Field(
        description="Seconds of free-form messaging left. Zero when the window is closed."
    )
    fallback_template: str | None = Field(
        default=None,
        description=(
            "The template that will be sent instead once the window closes. Null means a send "
            "outside the window is refused."
        ),
    )


class WebhookAckResponse(CamelModel):
    """What one delivery amounted to. For your logs — WhatsApp only reads the status code."""

    accepted: int = Field(description="Messages claimed and queued for an answer.")
    duplicates: int = Field(
        description="Messages already seen and ignored. A replayed delivery counts here."
    )
    statuses: int = Field(description="Delivery receipts applied to messages we sent.")
