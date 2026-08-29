"""What every WhatsApp provider must do, and the shapes it speaks in (spec §5.5).

The same bargain the LLM abstraction makes in ``src/shared/llm``: one interface, several vendors,
and switching between them is a credential change rather than a code change. Meta's Cloud API is the
provider v1 ships; Twilio and 360dialog are resellers of the same product with different envelopes,
so they are a file each behind this protocol — never a branch inside the service.

The line is drawn so that **everything vendor-specific is on this side of it**. A provider owns its
wire format, its authentication, its signature scheme, and its error vocabulary; what comes back out
is :class:`ParsedWebhook`, :class:`InboundMessage` and :class:`OutboundResult`, which say nothing
about who produced them. If the service ever has to ask which provider it is talking to, this
interface has failed.

Signature verification lives here for the same reason and one more: it happens *before* the body is
parsed, so it cannot be a method on something built from the parsed body. Meta signs the raw bytes
with the app secret (``X-Hub-Signature-256``); Twilio signs the URL plus sorted form fields. Those
have nothing in common except their purpose, which is exactly what an interface is for.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Protocol


class WhatsAppError(Exception):
    """A provider call failed.

    ``retryable`` separates "try again" from "this will never work". A 429 or a 502 from Meta is
    worth another attempt; a rejected template name or an expired token is not, and retrying it
    three times only means failing three times as slowly.
    """

    def __init__(self, message: str, *, retryable: bool = False, code: str | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class InboundKind(str, enum.Enum):
    """What a contact sent, normalised.

    ``UNSUPPORTED`` is a real outcome rather than an error: WhatsApp delivers stickers, contacts,
    locations, reactions and polls, and an agent that cannot read one should say so politely rather
    than have its webhook 500 and be redelivered forever.
    """

    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"
    VIDEO = "video"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class InboundMedia:
    """A file a contact sent, by reference. Nothing is downloaded until someone asks for it."""

    media_id: str
    media_type: str | None = None
    filename: str | None = None
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One message from a contact, in provider-neutral terms."""

    provider_message_id: str
    contact_id: str
    kind: InboundKind
    text: str = ""
    media: InboundMedia | None = None
    contact_name: str | None = None
    timestamp: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StatusUpdate:
    """A delivery receipt for a message we sent."""

    provider_message_id: str
    status: str
    timestamp: int | None = None
    error_detail: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedWebhook:
    """Everything one webhook delivery carried.

    A single POST can hold several messages *and* several receipts — Meta batches — so both are
    lists, and a delivery with neither is normal traffic (an account update) rather than an error.
    """

    messages: list[InboundMessage] = field(default_factory=list)
    statuses: list[StatusUpdate] = field(default_factory=list)
    # The number the delivery was addressed to. Checked against the connection's own, so a webhook
    # routed to the wrong tenant's URL is refused rather than answered by the wrong agent.
    phone_number_id: str | None = None


@dataclass(frozen=True, slots=True)
class OutboundResult:
    """What the provider said when it accepted a message."""

    provider_message_id: str | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TemplateMessage:
    """A pre-approved template, the only thing deliverable outside the 24-hour window (§5.5).

    ``variables`` fill the template's body placeholders in order — WhatsApp numbers them ``{{1}}``,
    ``{{2}}`` and so on, so a positional list is the honest representation rather than a dict whose
    keys would have to be numbers anyway.
    """

    name: str
    language: str = "en_US"
    variables: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MediaPayload:
    """Bytes downloaded from the provider, ready for the extraction path."""

    data: bytes
    media_type: str
    filename: str | None = None


class WhatsAppProvider(Protocol):
    """One WhatsApp Business account, as the rest of the module sees it."""

    name: str

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        """Whether this delivery really came from the provider. Constant-time."""
        ...

    def parse_webhook(self, body: dict[str, Any]) -> ParsedWebhook:
        """Turn one webhook body into messages and receipts. Never raises on unknown content."""
        ...

    async def send_text(self, to: str, text: str) -> OutboundResult: ...

    async def send_template(self, to: str, template: TemplateMessage) -> OutboundResult: ...

    async def send_media(
        self, to: str, media_url: str, kind: InboundKind, caption: str | None = None
    ) -> OutboundResult: ...

    async def fetch_media(self, media_id: str, max_bytes: int) -> MediaPayload:
        """Download a contact's attachment. Raises :class:`WhatsAppError` past ``max_bytes``."""
        ...

    async def mark_read(self, provider_message_id: str) -> None:
        """Show the contact their message was seen. Best effort — never fails a turn."""
        ...
