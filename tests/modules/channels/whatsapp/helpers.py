"""Shared scaffolding for the WhatsApp tests.

Two fakes and a signer. :class:`FakeProvider` stands in for Meta — it records what would have been
sent and answers with plausible ``wamid`` values — while :func:`meta_webhook` builds the real
four-level envelope Meta actually posts, so the parser is exercised against the shape it will meet
in production rather than a flattened convenience.

Signatures are computed with the genuine HMAC, not stubbed: verification is a security control, and
a test that bypasses it would pass just as happily if the control were removed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

from httpx import AsyncClient

from src.modules.channels.whatsapp.internal.providers.base import (
    InboundKind,
    MediaPayload,
    OutboundResult,
    ParsedWebhook,
    TemplateMessage,
    WhatsAppError,
)
from src.modules.channels.whatsapp.internal.providers.meta import MetaCloudProvider

APP_SECRET = "test-app-secret"
PHONE_NUMBER_ID = "109876543210987"
CONTACT = "263770000000"

CREDENTIALS: dict[str, Any] = {
    "provider": "meta",
    "phoneNumberId": PHONE_NUMBER_ID,
    "accessToken": "EAAG-test-token",
    "appSecret": APP_SECRET,
}

PUBLISHABLE: dict[str, Any] = {
    "persona": "You are the sales assistant for Nash Paints.",
    "modelProvider": "gemini",
    "modelSettings": {"model": "gemini-2.0-flash", "temperature": 0.5, "maxTokens": 512},
}


class FakeProvider:
    """A Meta account that never leaves the process.

    Parsing and signature verification are delegated to the *real* :class:`MetaCloudProvider`, so
    what is faked is only the network — the envelope walking and the HMAC under test stay the code
    that ships.
    """

    name = "meta"

    def __init__(
        self,
        app_secret: str = APP_SECRET,
        media: MediaPayload | None = None,
        fail_with: WhatsAppError | None = None,
    ) -> None:
        self._real = MetaCloudProvider(
            phone_number_id=PHONE_NUMBER_ID, access_token="t", app_secret=app_secret
        )
        self.media = media
        self.fail_with = fail_with
        self.sent_text: list[tuple[str, str]] = []
        self.sent_templates: list[tuple[str, TemplateMessage]] = []
        self.sent_media: list[tuple[str, str, InboundKind, str | None]] = []
        self.marked_read: list[str] = []
        self.fetched: list[str] = []

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        return self._real.verify_signature(raw_body, headers)

    def parse_webhook(self, body: dict[str, Any]) -> ParsedWebhook:
        return self._real.parse_webhook(body)

    async def send_text(self, to: str, text: str) -> OutboundResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent_text.append((to, text))
        return OutboundResult(provider_message_id=f"wamid.out.{uuid.uuid4().hex[:10]}")

    async def send_template(self, to: str, template: TemplateMessage) -> OutboundResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent_templates.append((to, template))
        return OutboundResult(provider_message_id=f"wamid.tpl.{uuid.uuid4().hex[:10]}")

    async def send_media(
        self, to: str, media_url: str, kind: InboundKind, caption: str | None = None
    ) -> OutboundResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent_media.append((to, media_url, kind, caption))
        return OutboundResult(provider_message_id=f"wamid.media.{uuid.uuid4().hex[:10]}")

    async def fetch_media(self, media_id: str, max_bytes: int) -> MediaPayload:
        self.fetched.append(media_id)
        if self.media is None:
            raise WhatsAppError("No media staged for this test.")
        if len(self.media.data) > max_bytes:
            raise WhatsAppError("Too large.", code="MEDIA_TOO_LARGE")
        return self.media

    async def mark_read(self, provider_message_id: str) -> None:
        self.marked_read.append(provider_message_id)


# -- Meta's wire format ------------------------------------------------------------------


def meta_webhook(
    *,
    message_id: str = "wamid.HBgMMjYzNzcwMDAwMDAwFQIAEhgg",
    text: str | None = "Do you have white emulsion in 20 litres?",
    contact: str = CONTACT,
    phone_number_id: str = PHONE_NUMBER_ID,
    media_kind: str | None = None,
    media_id: str = "media-1",
    media_type: str = "image/jpeg",
    filename: str | None = None,
    caption: str | None = None,
    statuses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One delivery, in the shape Meta actually posts."""
    value: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "263770000001", "phone_number_id": phone_number_id},
    }

    if text is not None or media_kind is not None:
        value["contacts"] = [{"profile": {"name": "Tariro"}, "wa_id": contact}]
        message: dict[str, Any] = {
            "from": contact,
            "id": message_id,
            "timestamp": "1735689600",
        }
        if media_kind is not None:
            payload: dict[str, Any] = {"id": media_id, "mime_type": media_type}
            if filename:
                payload["filename"] = filename
            if caption:
                payload["caption"] = caption
            message["type"] = media_kind
            message[media_kind] = payload
        else:
            message["type"] = "text"
            message["text"] = {"body": text}
        value["messages"] = [message]

    if statuses:
        value["statuses"] = statuses

    change = {"field": "messages", "value": value}
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "business-account-id", "changes": [change]}],
    }


def signed(body: dict[str, Any], secret: str = APP_SECRET) -> tuple[bytes, dict[str, str]]:
    """Serialise once and sign those exact bytes — the way a receiver must verify them."""
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


async def post_webhook(
    client: AsyncClient, connection_id: str, body: dict[str, Any], secret: str = APP_SECRET
) -> Any:
    raw, headers = signed(body, secret)
    return await client.post(
        f"/v1/channels/whatsapp/webhook/{connection_id}", content=raw, headers=headers
    )


# -- tenant setup ------------------------------------------------------------------------


async def connected_agent(
    client: AsyncClient,
    auth: dict[str, str],
    publish: bool = True,
    **connect: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a published agent with a WhatsApp connection. Returns ``(agent, connection)``."""
    created = await client.post(
        "/agents", json={"name": f"Agent {uuid.uuid4().hex[:6]}", **PUBLISHABLE}, headers=auth
    )
    assert created.status_code == 201, created.text
    agent: dict[str, Any] = created.json()["value"]

    if publish:
        published = await client.post(f"/agents/{agent['id']}/publish", headers=auth)
        assert published.status_code == 200, published.text

    payload = {
        "provider": "meta",
        "phoneNumberId": PHONE_NUMBER_ID,
        "accessToken": CREDENTIALS["accessToken"],
        "appSecret": APP_SECRET,
        **connect,
    }
    response = await client.put(
        f"/agents/{agent['id']}/channels/whatsapp", json=payload, headers=auth
    )
    assert response.status_code == 200, response.text
    connection: dict[str, Any] = response.json()["value"]
    return agent, connection
