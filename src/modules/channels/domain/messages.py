"""The channel-agnostic internal message format (spec §5.5).

Every channel adapter maps its own wire format onto :class:`IncomingMessage` and renders
:class:`OutgoingMessage` back out. Nothing downstream of the adapter — not the conversation engine,
not retrieval, not the guardrails — knows whether a message arrived from a website widget or from
WhatsApp.

That indirection earns its keep in Phase 10. WhatsApp brings a 24-hour session window, template
messages, delivery receipts and media, and the reason those can be added as an adapter rather than
as a rewrite is that the engine only ever sees this shape.

Deliberately *not* a lowest common denominator. ``attachments`` and ``channel_metadata`` exist here
even though the web channel barely uses them, because the alternative — adding them when WhatsApp
arrives — means changing the format every channel already depends on.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class MessageKind(str, enum.Enum):
    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class Attachment:
    """A file that came with a message. Carried by reference — nothing is downloaded here."""

    kind: MessageKind
    url: str | None = None
    media_type: str | None = None
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """One inbound message, in the terms the engine understands.

    ``external_user_id`` is whatever identifies the speaker on that channel — a browser session
    id on the web, a phone number on WhatsApp. It is never parsed: it is a session key, no more.

    ``idempotency_key`` is the channel's own id for a message, where it has one. WhatsApp
    redelivers webhooks, and answering the same message twice is the failure that causes (§6). The
    web channel lets a caller supply one so a retried POST does not produce a second reply.
    """

    agent_id: Any
    channel: str
    external_user_id: str
    text: str
    kind: MessageKind = MessageKind.TEXT
    attachments: list[Attachment] = field(default_factory=list)
    idempotency_key: str | None = None
    channel_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    """One reply, ready for a channel to render however it needs to."""

    conversation_id: Any
    text: str
    kind: MessageKind = MessageKind.TEXT
    escalated: bool = False
    citations: list[dict[str, Any]] = field(default_factory=list)
    channel_metadata: dict[str, Any] = field(default_factory=dict)
