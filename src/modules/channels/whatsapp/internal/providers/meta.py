"""Meta's WhatsApp Cloud API (spec §5.5).

The provider v1 ships. Everything specific to Meta is here: the Graph endpoint shape, the
``X-Hub-Signature-256`` scheme, the deeply nested webhook envelope, and the two-step media download.

Three of those are worth knowing about before changing anything.

**The signature covers the raw bytes.** ``sha256=<hex hmac>`` over the body exactly as it arrived —
not over a re-serialised parse of it. A JSON round-trip changes whitespace and key order, and the
signature then fails for reasons that look like a wrong secret. That is why the controller reads
``await request.body()`` and hands the bytes down here untouched.

**The webhook envelope is four levels deep and every level is a list**:
``entry[] -> changes[] -> value -> messages[] | statuses[]``. Meta batches, so one POST can carry
several messages from different contacts, and parsing has to walk all of it rather than reach for
``[0]``. Everything is read defensively — a field Meta adds tomorrow must not raise here, because a
webhook that 500s is a webhook that gets redelivered forever.

**Media is two calls, not one.** ``GET /{media-id}`` returns a short-lived signed URL on a
different host; fetching that URL still needs the bearer token. Both steps are size-capped, and the
cap is checked against the declared size *and* against what actually arrives — a lying or absent
header must not be able to pull an unbounded body into memory.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

from src import configs
from src.modules.channels.whatsapp.internal.providers.base import (
    InboundKind,
    InboundMedia,
    InboundMessage,
    MediaPayload,
    OutboundResult,
    ParsedWebhook,
    StatusUpdate,
    TemplateMessage,
    WhatsAppError,
)

logger = logging.getLogger("api.channels.whatsapp.meta")

SIGNATURE_HEADER = "x-hub-signature-256"
SIGNATURE_PREFIX = "sha256="

# Meta's message types mapped onto ours. Anything absent — stickers, locations, contacts, reactions,
# polls, system notices — is UNSUPPORTED, which the agent answers rather than crashes on.
_KINDS: dict[str, InboundKind] = {
    "text": InboundKind.TEXT,
    "image": InboundKind.IMAGE,
    "document": InboundKind.DOCUMENT,
    "audio": InboundKind.AUDIO,
    "video": InboundKind.VIDEO,
}

# Graph errors worth another attempt: throttling and Meta's own transient failures. Everything else
# (a bad token, an unapproved template, a number that is not on WhatsApp) fails the same way twice.
_RETRYABLE_CODES = frozenset({"4", "80007", "130429", "131048", "131056", "368", "500", "1", "2"})


class MetaCloudProvider:
    """One WhatsApp Business phone number on Meta's Cloud API."""

    name = "meta"

    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        app_secret: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.app_secret = app_secret
        self._client = client

    # -- verification --------------------------------------------------------

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        """HMAC-SHA256 of the raw body under the app secret, compared in constant time.

        A connection with no app secret configured cannot verify anything, and this returns False
        rather than True: an unverifiable webhook is refused, because the alternative is a public
        endpoint that runs a tenant's agent for anyone who finds the URL.
        """
        if not self.app_secret:
            return False

        provided = _header(headers, SIGNATURE_HEADER)
        if not provided or not provided.startswith(SIGNATURE_PREFIX):
            return False

        expected = hmac.new(self.app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, provided[len(SIGNATURE_PREFIX) :])

    # -- inbound -------------------------------------------------------------

    def parse_webhook(self, body: dict[str, Any]) -> ParsedWebhook:
        """Walk ``entry[] -> changes[] -> value`` and collect every message and receipt.

        Deliberately total: unknown shapes are skipped, not raised on. Meta redelivers anything it
        does not get a 200 for, so an exception here would turn one malformed delivery into an
        unbounded retry loop.
        """
        messages: list[InboundMessage] = []
        statuses: list[StatusUpdate] = []
        phone_number_id: str | None = None

        for entry in _items(body.get("entry")):
            for change in _items(entry.get("changes")):
                value = change.get("value")
                if not isinstance(value, dict):
                    continue

                metadata = value.get("metadata")
                if isinstance(metadata, dict):
                    phone_number_id = str(metadata.get("phone_number_id") or "") or phone_number_id

                names = _contact_names(value)
                for raw in _items(value.get("messages")):
                    parsed = self._message(raw, names)
                    if parsed is not None:
                        messages.append(parsed)

                for raw in _items(value.get("statuses")):
                    parsed_status = self._status(raw)
                    if parsed_status is not None:
                        statuses.append(parsed_status)

        return ParsedWebhook(messages=messages, statuses=statuses, phone_number_id=phone_number_id)

    def _message(self, raw: dict[str, Any], names: dict[str, str]) -> InboundMessage | None:
        message_id = str(raw.get("id") or "")
        contact_id = str(raw.get("from") or "")
        if not message_id or not contact_id:
            # Without an id there is no idempotency key, and without a sender there is nobody to
            # answer. Either way this is not a message we can safely act on.
            return None

        declared = str(raw.get("type") or "")
        kind = _KINDS.get(declared, InboundKind.UNSUPPORTED)
        text = ""
        media: InboundMedia | None = None

        if kind is InboundKind.TEXT:
            payload = raw.get("text")
            text = str(payload.get("body") or "") if isinstance(payload, dict) else ""
        elif kind is not InboundKind.UNSUPPORTED:
            payload = raw.get(declared)
            if isinstance(payload, dict) and payload.get("id"):
                caption = payload.get("caption")
                media = InboundMedia(
                    media_id=str(payload["id"]),
                    media_type=_string_or_none(payload.get("mime_type")),
                    filename=_string_or_none(payload.get("filename")),
                    caption=_string_or_none(caption),
                )
                text = str(caption or "")
            else:
                kind = InboundKind.UNSUPPORTED

        return InboundMessage(
            provider_message_id=message_id,
            contact_id=contact_id,
            kind=kind,
            text=text,
            media=media,
            contact_name=names.get(contact_id),
            timestamp=_int_or_none(raw.get("timestamp")),
            raw=raw,
        )

    def _status(self, raw: dict[str, Any]) -> StatusUpdate | None:
        message_id = str(raw.get("id") or "")
        status = str(raw.get("status") or "")
        if not message_id or not status:
            return None

        detail: str | None = None
        errors = _items(raw.get("errors"))
        if errors:
            first = errors[0]
            detail = str(first.get("title") or first.get("message") or "")[:500] or None

        return StatusUpdate(
            provider_message_id=message_id,
            status=status,
            timestamp=_int_or_none(raw.get("timestamp")),
            error_detail=detail,
        )

    # -- outbound ------------------------------------------------------------

    async def send_text(self, to: str, text: str) -> OutboundResult:
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                # `preview_url` off: a link in an agent's answer should not silently fetch a page
                # on the contact's behalf, and an unfurled preview of a KB citation is noise.
                "text": {"preview_url": False, "body": text},
            }
        )

    async def send_template(self, to: str, template: TemplateMessage) -> OutboundResult:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {"name": template.name, "language": {"code": template.language}},
        }
        if template.variables:
            payload["template"]["components"] = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": value} for value in template.variables],
                }
            ]
        return await self._send(payload)

    async def send_media(
        self, to: str, media_url: str, kind: InboundKind, caption: str | None = None
    ) -> OutboundResult:
        if kind in (InboundKind.TEXT, InboundKind.UNSUPPORTED):
            raise WhatsAppError(f"{kind.value} is not a media type.", code="UNSUPPORTED_MEDIA")

        body: dict[str, Any] = {"link": media_url}
        # Audio is the one type Meta rejects a caption on, so it is dropped rather than sent and
        # refused with an error a tenant would have to decode.
        if caption and kind is not InboundKind.AUDIO:
            body["caption"] = caption

        return await self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": kind.value,
                kind.value: body,
            }
        )

    async def mark_read(self, provider_message_id: str) -> None:
        """Best effort: a failed read receipt is cosmetic and must never fail a turn."""
        try:
            await self._send(
                {
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": provider_message_id,
                }
            )
        except WhatsAppError as exc:
            logger.debug("could not mark %s read: %s", provider_message_id, exc)

    # -- media ---------------------------------------------------------------

    async def fetch_media(self, media_id: str, max_bytes: int) -> MediaPayload:
        """Resolve the media id to a signed URL, then download it under a hard size cap."""
        client, owned = self._http()
        try:
            described = await self._request(
                client, "GET", f"{self._graph}/{media_id}", headers=self._auth
            )
            url = str(described.get("url") or "")
            if not url:
                raise WhatsAppError("The provider returned no download URL for that attachment.")

            declared = _int_or_none(described.get("file_size")) or 0
            if declared > max_bytes:
                raise WhatsAppError(
                    f"That attachment is {declared} bytes, over the {max_bytes} byte limit.",
                    code="MEDIA_TOO_LARGE",
                )

            # Streamed, so a body larger than the cap is abandoned rather than read into memory —
            # `file_size` above is the provider's claim, and this is the check that does not
            # depend on it being true.
            data = bytearray()
            async with client.stream("GET", url, headers=self._auth) as response:
                if response.status_code >= 400:
                    raise WhatsAppError(
                        f"Downloading the attachment failed (HTTP {response.status_code}).",
                        retryable=response.status_code >= 500,
                    )
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise WhatsAppError(
                            f"That attachment is over the {max_bytes} byte limit.",
                            code="MEDIA_TOO_LARGE",
                        )
                media_type = response.headers.get("content-type", "").split(";")[0].strip() or str(
                    described.get("mime_type") or "application/octet-stream"
                )

            return MediaPayload(
                data=bytes(data),
                media_type=media_type,
                filename=_string_or_none(described.get("file_name")),
            )
        finally:
            if owned:
                await client.aclose()

    # -- transport -----------------------------------------------------------

    @property
    def _graph(self) -> str:
        base = str(configs.WHATSAPP_API_BASE_URL).rstrip("/")
        return f"{base}/{configs.WHATSAPP_API_VERSION}"

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _http(self) -> tuple[httpx.AsyncClient, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.AsyncClient(timeout=configs.WHATSAPP_REQUEST_TIMEOUT_SECONDS), True

    async def _send(self, payload: dict[str, Any]) -> OutboundResult:
        client, owned = self._http()
        try:
            body = await self._request(
                client,
                "POST",
                f"{self._graph}/{self.phone_number_id}/messages",
                headers={**self._auth, "Content-Type": "application/json"},
                json=payload,
            )
        finally:
            if owned:
                await client.aclose()

        sent = _items(body.get("messages"))
        return OutboundResult(
            provider_message_id=str(sent[0].get("id")) if sent and sent[0].get("id") else None,
            raw=body,
        )

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One Graph call. Every failure comes back as a :class:`WhatsAppError`.

        Meta reports failures in the body as much as in the status line, so the error message a
        tenant reads is Meta's own — "Template name does not exist in the translation" is worth
        more to them than "HTTP 400".
        """
        try:
            response = await client.request(method, url, headers=headers, json=json)
        except httpx.HTTPError as exc:
            raise WhatsAppError(
                f"Could not reach WhatsApp: {type(exc).__name__}.", retryable=True
            ) from exc

        try:
            parsed = response.json()
        except ValueError:
            parsed = {}
        body: dict[str, Any] = parsed if isinstance(parsed, dict) else {}

        if response.status_code >= 400:
            raw_error = body.get("error")
            error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
            code = _string_or_none(error.get("code"))
            fallback = f"WhatsApp rejected the request (HTTP {response.status_code})."
            raise WhatsAppError(
                str(error.get("message") or fallback)[:500],
                retryable=response.status_code >= 500 or code in _RETRYABLE_CODES,
                code=code,
            )

        return body


# -- parsing helpers ---------------------------------------------------------------------


def _items(value: Any) -> list[dict[str, Any]]:
    """Every list in Meta's envelope, read without trusting its shape."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _contact_names(value: dict[str, Any]) -> dict[str, str]:
    """WhatsApp id to the display name the contact chose, where the delivery carried one."""
    names: dict[str, str] = {}
    for contact in _items(value.get("contacts")):
        wa_id = str(contact.get("wa_id") or "")
        profile = contact.get("profile")
        if wa_id and isinstance(profile, dict) and profile.get("name"):
            names[wa_id] = str(profile["name"])
    return names


def _header(headers: dict[str, str], name: str) -> str:
    """Case-insensitive lookup — header casing is not guaranteed across proxies."""
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return ""


def _string_or_none(value: Any) -> str | None:
    text = str(value if value is not None else "").strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
