"""The WhatsApp channel (spec §5.5, §6).

This is the second caller of ``ChannelService.handle`` — the seam Phase 8 built the
channel-agnostic message format for. Everything below the adapter is unchanged: the same
guardrails, the same retrieval, the same prompt, the same stored conversation. What WhatsApp adds
is entirely on this side of that line, and it is the three things §6 names as easy to get wrong.

**Idempotency.** ``record_inbound`` inserts the message row *before* any work happens, inside a
savepoint. If the unique constraint on ``(connection_id, provider_message_id)`` rejects the insert,
this delivery is a replay and the method returns ``None`` — no turn, no reply, no token spent. That
ordering is the whole mechanism: checking first and inserting later leaves a gap two workers can
both walk through. WhatsApp redelivers whenever it does not see a fast ``200``, so this is not a
rare path.

**The 24-hour window.** Decided in ``internal/session_window.py`` before anything is sent, and a
closed window falls back to the connection's configured template. See that module for why.

**Media.** An inbound image or document is downloaded from the provider and read through
``KnowledgeBaseService.extract_text`` — the same extraction path an upload takes, reached service to
service. The text it produces is *content the contact sent*, so it goes into the user message and
through the same fencing every user message gets (§5.7): a photographed instruction is still an
instruction someone else wrote, and the engine must treat it as data.

**Nothing here fails a webhook.** Meta reads a non-200 as "try again", so a failure that would be
redelivered forever is instead recorded on the message row and answered with 200. The tenant sees
it in their delivery log; Meta sees success and stops.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src import configs
from src.core import queue
from src.modules.agents.domain.services import AgentService
from src.modules.channels.domain.messages import (
    Attachment,
    IncomingMessage,
    MessageKind,
    OutgoingMessage,
)
from src.modules.channels.domain.models import ChannelConfig, ChannelStatus, ChannelType
from src.modules.channels.domain.services import ChannelService
from src.modules.channels.whatsapp.domain.models import (
    DeliveryStatus,
    MessageDirection,
    MessageType,
    WhatsAppMessage,
)
from src.modules.channels.whatsapp.domain.repositories import (
    WhatsAppMessageRepository,
    find_by_provider_message_id,
)
from src.modules.channels.whatsapp.internal import connection as connection_fields
from src.modules.channels.whatsapp.internal import session_window, tasks
from src.modules.channels.whatsapp.internal.providers import (
    InboundKind,
    InboundMessage,
    ParsedWebhook,
    StatusUpdate,
    TemplateMessage,
    WhatsAppError,
    WhatsAppProvider,
    build_provider,
    required_credentials,
)
from src.modules.channels.whatsapp.internal.session_window import SessionWindow
from src.modules.conversations.domain.services import ConversationService
from src.modules.knowledge_base.domain.services import KnowledgeBaseService
from src.shared.database.pagination import Page, PageRequest
from src.shared.exceptions import (
    ConflictException,
    ServiceUnavailableException,
    ValidationException,
)

logger = logging.getLogger("api.channels.whatsapp")

# What the agent says when a contact sends something v1 cannot read — a sticker, a location, a
# voice note. A plain sentence rather than silence: the contact is waiting, and "we did not
# understand that" is information, whereas no reply looks like the number is dead.
UNSUPPORTED_REPLY = (
    "Sorry — I can only read text, images, and documents at the moment. "
    "Could you send that as a message or a file?"
)

# How an attachment's extracted words are introduced to the engine. Labelled as an attachment so
# the model can tell what the contact typed from what their file happened to contain — the message
# is fenced as data downstream either way (§5.7).
ATTACHMENT_TEMPLATE = "[The customer sent an attachment. Its contents:]\n{text}"

# Kinds we can turn into text. Audio and video have no v1 extraction path (§5.2.3 leans on native
# LLM file reading, which covers documents and images, not transcription).
READABLE_KINDS = frozenset({InboundKind.IMAGE, InboundKind.DOCUMENT})

_KIND_TO_TYPE: dict[InboundKind, MessageType] = {
    InboundKind.TEXT: MessageType.TEXT,
    InboundKind.IMAGE: MessageType.IMAGE,
    InboundKind.DOCUMENT: MessageType.DOCUMENT,
    InboundKind.AUDIO: MessageType.AUDIO,
    InboundKind.VIDEO: MessageType.VIDEO,
    InboundKind.UNSUPPORTED: MessageType.UNSUPPORTED,
}

_KIND_TO_ATTACHMENT: dict[InboundKind, MessageKind] = {
    InboundKind.IMAGE: MessageKind.IMAGE,
    InboundKind.DOCUMENT: MessageKind.DOCUMENT,
}

# Meta's receipt vocabulary, and the column each one lands in.
_STATUS_MAP: dict[str, DeliveryStatus] = {
    "sent": DeliveryStatus.SENT,
    "delivered": DeliveryStatus.DELIVERED,
    "read": DeliveryStatus.READ,
    "failed": DeliveryStatus.FAILED,
}


@dataclass(frozen=True, slots=True)
class OutboundMedia:
    """A file to send to a contact, by URL.

    A link rather than bytes: WhatsApp fetches the URL itself, so uploading through us would mean
    holding a tenant's file twice and paying to move it twice. The URL must be publicly reachable —
    that is Meta's requirement, not ours, and the send fails with their own message if it is not.
    """

    url: str
    kind: InboundKind
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class ReceiveResult:
    """What one webhook delivery amounted to, for the acknowledgement and the logs."""

    accepted: int = 0
    duplicates: int = 0
    statuses: int = 0


class WhatsAppService:
    """One tenant's WhatsApp channels: connection, inbound turns, and outbound sends."""

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        provider: WhatsAppProvider | None = None,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.messages = WhatsAppMessageRepository(session, tenant_id)
        self.channels = ChannelService(session, tenant_id)
        self.agents = AgentService(session, tenant_id)
        # Injected only by tests. Production always builds one from the connection's own
        # credentials, so two tenants can be on different providers in the same process.
        self._provider = provider

    def provider_for(self, connection: ChannelConfig) -> WhatsAppProvider:
        if self._provider is not None:
            return self._provider
        try:
            return build_provider(dict(connection.credentials_json))
        except WhatsAppError as exc:
            raise ConflictException(str(exc), code="WHATSAPP_CONNECTION_INCOMPLETE") from exc

    # -- connection ----------------------------------------------------------

    async def connect(
        self,
        agent_id: uuid.UUID,
        credentials: dict[str, Any],
        settings: dict[str, Any] | None = None,
        status: ChannelStatus | None = None,
    ) -> ChannelConfig:
        """Create or update an agent's WhatsApp connection (spec §5.5, per-agent connection flow).

        Credentials are validated before they are stored, so an incomplete connection is refused at
        the moment a tenant saves it rather than at the moment a customer messages. The verify token
        is generated here if the tenant has none — it is compared against an untrusted string on a
        public endpoint, so it is not something to let anyone choose.
        """
        agent = await self.agents.get(agent_id)
        existing = await self.channels.configs.for_agent(agent.id, ChannelType.WHATSAPP)

        merged = connection_fields.merge_credentials(
            dict(existing.credentials_json) if existing else {}, credentials
        )
        self._assert_credentials_complete(merged)

        config = await self.channels.configure(
            agent.id,
            ChannelType.WHATSAPP,
            settings=settings
            if settings is not None
            else (existing.settings_json if existing else {}),
            credentials=merged,
        )
        if status is not None and config.status is not status:
            # Not part of `configure`'s signature, which is shared with the web channel and has no
            # reason to grow a status argument for one caller.
            config = await self.channels.configs.update(config, status=status)
        logger.info("whatsapp connection %s configured for agent %s", config.id, agent.id)
        return config

    async def get_connection(self, agent_id: uuid.UUID) -> ChannelConfig:
        return await self.channels.get_config(agent_id, ChannelType.WHATSAPP)

    async def disconnect(self, agent_id: uuid.UUID) -> None:
        """Remove the connection and its credentials. Inbound webhooks stop resolving at once."""
        connection = await self.get_connection(agent_id)
        await self.channels.configs.delete(connection)
        logger.info("whatsapp connection %s removed for agent %s", connection.id, agent_id)

    def _assert_credentials_complete(self, credentials: dict[str, Any]) -> None:
        name = str(credentials.get(connection_fields.PROVIDER) or "meta").strip().lower()
        try:
            required = required_credentials(name)
        except WhatsAppError as exc:
            raise ValidationException(str(exc), code="WHATSAPP_UNKNOWN_PROVIDER") from exc

        missing = [key for key in required if not str(credentials.get(key) or "").strip()]
        if missing:
            raise ValidationException(
                f"These credentials are required for the {name} provider: {', '.join(missing)}.",
                code="WHATSAPP_INCOMPLETE_CREDENTIALS",
            )

    # -- webhook verification ------------------------------------------------

    def verify_handshake(self, connection: ChannelConfig, mode: str, token: str) -> bool:
        """Meta's subscribe handshake (spec §5.5, "webhook receipt + verification").

        Compared in constant time. A plain ``==`` on a secret leaks its prefix through timing, and
        this endpoint is public and unauthenticated by definition — it is what *establishes* the
        webhook, so there is nothing else guarding it.
        """
        import hmac

        expected = str(connection.credentials_json.get(connection_fields.VERIFY_TOKEN) or "")
        return bool(expected) and mode == "subscribe" and hmac.compare_digest(expected, token)

    # -- inbound -------------------------------------------------------------

    def parse(self, connection: ChannelConfig, body: dict[str, Any]) -> ParsedWebhook:
        return self.provider_for(connection).parse_webhook(body)

    async def record_inbound(
        self, connection: ChannelConfig, message: InboundMessage
    ) -> WhatsAppMessage | None:
        """Claim a message, or discover it was already claimed.

        Returns the new row, or ``None`` when this ``wamid`` has been seen before — which is the
        replay case and the point of the whole method. The insert is wrapped in a savepoint because
        a failed insert poisons the surrounding transaction in SQLAlchemy: without it, the
        constraint doing its job would take the rest of the webhook down with it.
        """
        row = WhatsAppMessage(
            connection_id=connection.id,
            agent_id=connection.agent_id,
            direction=MessageDirection.INBOUND,
            provider_message_id=message.provider_message_id,
            wa_contact_id=message.contact_id,
            message_type=_KIND_TO_TYPE[message.kind],
            status=DeliveryStatus.RECEIVED,
            body=message.text or None,
            meta_json=_inbound_meta(message),
        )
        row.tenant_id = self.tenant_id

        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            logger.info(
                "duplicate whatsapp webhook for %s on connection %s; ignored",
                message.provider_message_id,
                connection.id,
            )
            return None

        return row

    async def handle_inbound(
        self,
        connection: ChannelConfig,
        record: WhatsAppMessage,
        message: InboundMessage,
        conversations: ConversationService,
    ) -> WhatsAppMessage | None:
        """Run the turn for one claimed inbound message and send the reply.

        Returns the outbound row, or ``None`` when nothing was sent — auto-reply switched off, or a
        failure already recorded on ``record``. Never raises: the caller is a webhook or a worker,
        and in both cases an exception would mean redelivery of a message already answered.
        """
        provider = self.provider_for(connection)
        settings = dict(connection.settings_json)

        if connection.status is ChannelStatus.DISABLED:
            await self._mark(record, DeliveryStatus.PROCESSED, "The channel is disabled.")
            return None

        if connection_fields.mark_read_enabled(settings):
            await provider.mark_read(message.provider_message_id)

        if not connection_fields.auto_reply_enabled(settings):
            # Recorded, not answered: the tenant is handling this number themselves, and the
            # message still belongs in their log.
            await self._mark(record, DeliveryStatus.PROCESSED)
            return None

        try:
            text, attachments = await self._readable(connection, message)
        except (WhatsAppError, ValidationException) as exc:
            await self._mark(record, DeliveryStatus.FAILED, str(exc))
            return await self._reply(connection, record, message.contact_id, UNSUPPORTED_REPLY)

        if not text.strip():
            await self._mark(record, DeliveryStatus.PROCESSED, "Nothing readable in the message.")
            return await self._reply(connection, record, message.contact_id, UNSUPPORTED_REPLY)

        try:
            outgoing = await self.channels.handle(
                IncomingMessage(
                    agent_id=connection.agent_id,
                    channel=ChannelType.WHATSAPP.value,
                    external_user_id=message.contact_id,
                    text=text,
                    kind=_attachment_kind(message.kind),
                    attachments=attachments,
                    idempotency_key=message.provider_message_id,
                    channel_metadata={"contactName": message.contact_name},
                ),
                conversations,
            )
        except Exception as exc:
            # Deliberately broad. The caller is a webhook or a worker, and letting anything at
            # all escape means Meta redelivers a message that may already have been answered.
            # The failure is recorded where the tenant can read it instead.
            logger.exception("whatsapp turn failed for message %s", record.id)
            await self._mark(record, DeliveryStatus.FAILED, str(exc)[:500])
            return None

        await self.messages.update(
            record,
            status=DeliveryStatus.PROCESSED,
            conversation_id=outgoing.conversation_id,
        )
        return await self._reply(
            connection, record, message.contact_id, outgoing.text, outgoing=outgoing
        )

    async def _readable(
        self, connection: ChannelConfig, message: InboundMessage
    ) -> tuple[str, list[Attachment]]:
        """Turn one inbound message into text the engine can answer, plus its attachments."""
        if message.kind is InboundKind.TEXT:
            return message.text, []

        if message.kind not in READABLE_KINDS or message.media is None:
            raise WhatsAppError(
                f"A {message.kind.value} message cannot be read.", code="UNSUPPORTED_MESSAGE_TYPE"
            )

        provider = self.provider_for(connection)
        payload = await provider.fetch_media(
            message.media.media_id, configs.WHATSAPP_MAX_MEDIA_BYTES
        )
        knowledge = KnowledgeBaseService(self.session, self.tenant_id)
        extracted = await knowledge.extract_text(
            message.media.filename or _filename_for(payload.media_type),
            payload.data,
            payload.media_type,
        )

        caption = (message.media.caption or "").strip()
        body = ATTACHMENT_TEMPLATE.format(text=extracted.strip())
        text = f"{caption}\n\n{body}" if caption else body

        return text, [
            Attachment(
                kind=_attachment_kind(message.kind),
                media_type=payload.media_type,
                filename=message.media.filename,
            )
        ]

    async def _reply(
        self,
        connection: ChannelConfig,
        record: WhatsAppMessage,
        contact_id: str,
        text: str,
        outgoing: OutgoingMessage | None = None,
    ) -> WhatsAppMessage | None:
        """Send the agent's answer back, recording the attempt either way."""
        try:
            return await self.send(
                connection,
                contact_id,
                text=text,
                conversation_id=outgoing.conversation_id if outgoing else record.conversation_id,
            )
        except (ConflictException, ServiceUnavailableException) as exc:
            logger.warning("could not deliver whatsapp reply to %s: %s", contact_id, exc)
            return None

    async def receive(
        self,
        connection: ChannelConfig,
        parsed: ParsedWebhook,
        conversations: ConversationService,
    ) -> ReceiveResult:
        """Claim everything one webhook delivery carried, and get the answers moving.

        The fast path, and it is deliberately short: claim each message, apply each receipt, and
        hand the turns to the queue. Meta gives a webhook a few seconds before it decides we are
        down and redelivers — and a turn is a retrieval plus a model call, which is not a few
        seconds. So the expensive part never happens here (§5.5: "webhook and ingestion paths stay
        fast by pushing work to the queue").

        Duplicates are counted rather than hidden, because "we received 40 webhooks and answered 12
        messages" is exactly what someone debugging a redelivery storm needs to see.
        """
        claimed: list[tuple[WhatsAppMessage, InboundMessage]] = []
        duplicates = 0

        for message in parsed.messages:
            record = await self.record_inbound(connection, message)
            if record is None:
                duplicates += 1
                continue
            claimed.append((record, message))

        statuses = 0
        for update in parsed.statuses:
            if await self.apply_status(connection, update) is not None:
                statuses += 1

        await self._dispatch(connection, claimed, conversations)
        return ReceiveResult(accepted=len(claimed), duplicates=duplicates, statuses=statuses)

    async def _dispatch(
        self,
        connection: ChannelConfig,
        claimed: list[tuple[WhatsAppMessage, InboundMessage]],
        conversations: ConversationService,
    ) -> None:
        """Run the claimed turns, or hand them to a worker. Only the mode differs.

        In ``redis`` mode the claims are **committed before they are enqueued**. A worker runs in
        its own transaction and its own process: enqueue first and it can look for a row this
        request has not written yet, find nothing, and drop a customer's message. Committing here
        is a transaction boundary, which is a service's to decide.
        """
        if not claimed:
            return

        if queue.is_inline():
            for record, message in claimed:
                await self.handle_inbound(connection, record, message, conversations)
            return

        await self.session.commit()
        for record, message in claimed:
            queue.enqueue(
                tasks.process_inbound_task,
                str(connection.id),
                str(record.id),
                tasks.to_payload(message),
            )

    # -- outbound ------------------------------------------------------------

    async def send(
        self,
        connection: ChannelConfig,
        contact_id: str,
        text: str | None = None,
        template: TemplateMessage | None = None,
        media: OutboundMedia | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> WhatsAppMessage:
        """Send one message, applying the 24-hour window rule (spec §5.5, §6).

        An explicit template is sent as asked — a template is always deliverable. Free-form text and
        media are sent only inside an open window; outside it, the connection's configured template
        is used instead, and if there is none the send is refused with ``WHATSAPP_WINDOW_CLOSED``
        rather than silently dropped.

        Media is not exempt from the window. An image is a free-form message as far as WhatsApp is
        concerned, so a photo sent to a closed window is refused or replaced exactly as text is —
        which is why the check happens before the branch on what kind of message this is.
        """
        if text is None and template is None and media is None:
            raise ValidationException(
                "Provide text, media, or a template.", code="WHATSAPP_NOTHING_TO_SEND"
            )

        window = await self.window_for(connection, contact_id)
        chosen = template
        body = text
        attachment = media

        if chosen is None and not window.is_open:
            chosen = self._fallback_template(connection)
            if chosen is None:
                raise ConflictException(
                    "This contact's 24-hour session window has closed, so only a pre-approved "
                    "template can reach them. Configure `outsideWindowTemplate` on the connection, "
                    "or wait for them to message first.",
                    code="WHATSAPP_WINDOW_CLOSED",
                )
            logger.info(
                "session window closed for %s; falling back to template %s",
                contact_id,
                chosen.name,
            )
            body = None
            attachment = None

        record = WhatsAppMessage(
            connection_id=connection.id,
            agent_id=connection.agent_id,
            conversation_id=conversation_id,
            direction=MessageDirection.OUTBOUND,
            wa_contact_id=contact_id,
            message_type=_outbound_type(chosen, attachment),
            status=DeliveryStatus.QUEUED,
            body=body if attachment is None else (attachment.caption or body),
            template_name=chosen.name if chosen else None,
            meta_json=(
                {"windowOpen": window.is_open}
                if attachment is None
                else {"windowOpen": window.is_open, "mediaUrl": attachment.url}
            ),
        )
        await self.messages.add(record)

        provider = self.provider_for(connection)
        try:
            if chosen is not None:
                result = await provider.send_template(contact_id, chosen)
            elif attachment is not None:
                result = await provider.send_media(
                    contact_id, attachment.url, attachment.kind, attachment.caption
                )
            else:
                result = await provider.send_text(contact_id, body or "")
        except WhatsAppError as exc:
            await self.messages.update(
                record, status=DeliveryStatus.FAILED, error_detail=str(exc)[:500]
            )
            if exc.retryable:
                raise ServiceUnavailableException(
                    str(exc), code="WHATSAPP_PROVIDER_UNAVAILABLE"
                ) from exc
            raise ConflictException(str(exc), code="WHATSAPP_SEND_REJECTED") from exc

        return await self.messages.update(
            record,
            provider_message_id=result.provider_message_id,
            status=DeliveryStatus.SENT,
            sent_at=datetime.now(UTC),
        )

    async def send_to_agent_contact(
        self,
        agent_id: uuid.UUID,
        contact_id: str,
        text: str | None = None,
        template: TemplateMessage | None = None,
        media: OutboundMedia | None = None,
    ) -> WhatsAppMessage:
        """The tenant-facing send: resolve the agent's connection, then send."""
        return await self.send(
            await self.get_connection(agent_id),
            contact_id,
            text=text,
            template=template,
            media=media,
        )

    def _fallback_template(self, connection: ChannelConfig) -> TemplateMessage | None:
        configured = connection_fields.template_from(dict(connection.settings_json))
        if configured is None:
            return None
        name, language, variables = configured
        return TemplateMessage(name=name, language=language, variables=variables)

    # -- delivery receipts ---------------------------------------------------

    async def apply_status(
        self, connection: ChannelConfig, update: StatusUpdate
    ) -> WhatsAppMessage | None:
        """Record what became of a message we sent.

        A receipt for a message we have no row for is normal — it may predate the connection, or
        belong to another system sharing the number — so it is ignored rather than treated as an
        error.
        """
        record = await find_by_provider_message_id(
            self.session, connection.id, update.provider_message_id
        )
        if record is None:
            return None

        status = _STATUS_MAP.get(update.status)
        if status is None:
            return record

        changes: dict[str, Any] = {"status": status}
        moment = _moment(update.timestamp)
        if status is DeliveryStatus.DELIVERED:
            changes["delivered_at"] = moment
        elif status is DeliveryStatus.READ:
            # A read receipt implies delivery, and Meta does not always send both.
            changes["read_at"] = moment
            if record.delivered_at is None:
                changes["delivered_at"] = moment
        elif status is DeliveryStatus.FAILED:
            changes["error_detail"] = (update.error_detail or "The provider rejected it.")[:500]

        return await self.messages.update(record, **changes)

    # -- reads ---------------------------------------------------------------

    async def window_for(self, connection: ChannelConfig, contact_id: str) -> SessionWindow:
        """Whether free-form text can reach this contact right now."""
        return session_window.evaluate(
            await self.messages.last_inbound_at(connection.id, contact_id)
        )

    async def window_for_agent(self, agent_id: uuid.UUID, contact_id: str) -> SessionWindow:
        return await self.window_for(await self.get_connection(agent_id), contact_id)

    async def list_messages(
        self,
        agent_id: uuid.UUID,
        page: PageRequest,
        direction: MessageDirection | None = None,
        status: DeliveryStatus | None = None,
        contact_id: str | None = None,
    ) -> Page[WhatsAppMessage]:
        connection = await self.get_connection(agent_id)
        return await self.messages.list_for_connection(
            connection.id, page, direction=direction, status=status, wa_contact_id=contact_id
        )

    async def _mark(
        self, record: WhatsAppMessage, status: DeliveryStatus, detail: str | None = None
    ) -> WhatsAppMessage:
        return await self.messages.update(
            record, status=status, error_detail=detail[:500] if detail else None
        )


# -- helpers -----------------------------------------------------------------------------


def _inbound_meta(message: InboundMessage) -> dict[str, Any]:
    meta: dict[str, Any] = {"kind": message.kind.value}
    if message.contact_name:
        meta["contactName"] = message.contact_name
    if message.timestamp is not None:
        meta["providerTimestamp"] = message.timestamp
    if message.media is not None:
        meta["mediaId"] = message.media.media_id
        if message.media.media_type:
            meta["mediaType"] = message.media.media_type
        if message.media.filename:
            meta["filename"] = message.media.filename
    return meta


def _outbound_type(template: TemplateMessage | None, media: OutboundMedia | None) -> MessageType:
    """What the delivery log should call this message."""
    if template is not None:
        return MessageType.TEMPLATE
    if media is not None:
        return _KIND_TO_TYPE.get(media.kind, MessageType.TEXT)
    return MessageType.TEXT


def _attachment_kind(kind: InboundKind) -> MessageKind:
    return _KIND_TO_ATTACHMENT.get(kind, MessageKind.TEXT)


def _filename_for(media_type: str) -> str:
    """A name for an attachment WhatsApp sent without one.

    Extraction picks its handler from the extension first and the declared type second, so an image
    arriving as ``image/jpeg`` with no filename still needs *something* with a suffix to route on.
    """
    suffix = media_type.split("/")[-1].split("+")[0] or "bin"
    return f"attachment.{'jpg' if suffix == 'jpeg' else suffix}"


def _moment(timestamp: int | None) -> datetime:
    """A provider timestamp as a datetime, falling back to now when it sent none or nonsense."""
    if timestamp is None:
        return datetime.now(UTC)
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return datetime.now(UTC)
