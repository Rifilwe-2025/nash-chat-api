"""Background work for the WhatsApp channel (spec §5.5, §4).

Same two-part shape as the knowledge base's tasks: an **async function** taking a session, which is
the real implementation and what the tests exercise, and a **Celery shell** that opens a session and
calls it. Nothing but the shell knows about Celery.

**Why the turn runs here at all.** Meta expects a webhook acknowledged in a few seconds and retries
anything slower — but a turn is a retrieval plus an LLM call, which is seconds on a good day. Doing
it inline would mean Meta timing out and redelivering a message that is *already being answered*, so
the fast path claims the message, returns 200, and hands the work over. That is the same reasoning
that moved ingestion off the request path in Phase 9, arrived at from the other direction.

**The claim happens in the request, not here.** ``record_inbound`` runs before the task is enqueued,
so the duplicate check has already been made and committed by the time a worker sees anything. A
task that runs twice — ``task_acks_late`` means it can — finds the row already processed and stops,
because it re-reads the row's status rather than trusting that it was queued once.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.queue import celery_app, run_async
from src.modules.channels.whatsapp.domain.models import DeliveryStatus, WhatsAppMessage
from src.modules.channels.whatsapp.domain.repositories import resolve_connection
from src.modules.channels.whatsapp.internal.providers.base import (
    InboundKind,
    InboundMedia,
    InboundMessage,
    WhatsAppProvider,
)
from src.modules.conversations.domain.services import ConversationService

logger = logging.getLogger("api.channels.whatsapp.tasks")

__all__ = [
    "process_inbound",
    "process_inbound_task",
    "to_payload",
]


def to_payload(message: InboundMessage) -> dict[str, Any]:
    """Flatten a parsed message for the broker.

    Celery carries JSON, so the dataclass is reduced to primitives rather than pickled — a queue
    that can only hold JSON is a queue whose messages stay readable when something goes wrong, and
    it keeps a worker from having to import a shape the producer happened to be running.
    """
    payload: dict[str, Any] = {
        "providerMessageId": message.provider_message_id,
        "contactId": message.contact_id,
        "kind": message.kind.value,
        "text": message.text,
        "contactName": message.contact_name,
        "timestamp": message.timestamp,
    }
    if message.media is not None:
        payload["media"] = {
            "mediaId": message.media.media_id,
            "mediaType": message.media.media_type,
            "filename": message.media.filename,
            "caption": message.media.caption,
        }
    return payload


def from_payload(payload: dict[str, Any]) -> InboundMessage:
    raw_media = payload.get("media")
    media = None
    if isinstance(raw_media, dict):
        media = InboundMedia(
            media_id=str(raw_media.get("mediaId") or ""),
            media_type=raw_media.get("mediaType"),
            filename=raw_media.get("filename"),
            caption=raw_media.get("caption"),
        )

    return InboundMessage(
        provider_message_id=str(payload.get("providerMessageId") or ""),
        contact_id=str(payload.get("contactId") or ""),
        kind=InboundKind(str(payload.get("kind") or InboundKind.TEXT.value)),
        text=str(payload.get("text") or ""),
        media=media,
        contact_name=payload.get("contactName"),
        timestamp=payload.get("timestamp"),
    )


async def process_inbound(
    session: AsyncSession,
    connection_id: uuid.UUID,
    record_id: uuid.UUID,
    payload: dict[str, Any],
    provider: WhatsAppProvider | None = None,
    llm_client: Any | None = None,
) -> WhatsAppMessage | None:
    """Answer one claimed inbound message.

    Returns the outbound row, or ``None`` when nothing was sent. Every early exit is a normal
    outcome rather than a failure: a connection deleted while the message waited, a row already
    processed by an earlier run of the same task, or auto-reply switched off.

    ``provider`` and ``llm_client`` are substitution points for tests, the same seam
    ``knowledge_base.internal.tasks.extract_source`` offers for its extractor. A worker passes
    neither and gets the real ones, built from the connection's own credentials.
    """
    connection = await resolve_connection(session, connection_id)
    if connection is None:
        logger.info("whatsapp connection %s no longer exists; dropping message", connection_id)
        return None

    record = await session.get(WhatsAppMessage, record_id)
    if record is None:
        return None
    if record.status is not DeliveryStatus.RECEIVED:
        # Already handled. `task_acks_late` can hand the same message to a second worker after the
        # first one died, and this is what stops that becoming a second reply.
        logger.info("whatsapp message %s is already %s; skipping", record_id, record.status.value)
        return None

    # Imported here, not at module scope: the service dispatches *to* this module, so importing
    # it at the top would close a cycle. The alternative — a registry or a callback passed in — buys
    # nothing over one deferred import at the single point of use.
    from src.modules.channels.whatsapp.domain.services import WhatsAppService

    service = WhatsAppService(session, connection.tenant_id, provider=provider)
    conversations = ConversationService(session, connection.tenant_id, llm_client=llm_client)

    try:
        return await service.handle_inbound(
            connection, record, from_payload(payload), conversations
        )
    except SoftTimeLimitExceeded:
        # Raised inside the task by Celery's soft limit, so the message records why it went
        # unanswered instead of being left in `received` for ever by the hard kill.
        record.status = DeliveryStatus.FAILED
        record.error_detail = "Answering the message took too long and was stopped."
        await session.flush()
        return None


@celery_app.task(
    name="whatsapp.process_inbound",
    bind=True,
    max_retries=2,
    default_retry_delay=10,
)
def process_inbound_task(
    self: Any, connection_id: str, record_id: str, payload: dict[str, Any]
) -> None:
    """Answer one message in a worker.

    Retried only twice, and briefly. A customer waiting on WhatsApp will have given up long before a
    third attempt on a thirty-second backoff, so a reply that has failed twice is better recorded as
    failed than delivered ten minutes late to someone who has moved on.
    """
    try:
        run_async(
            lambda session: process_inbound(
                session, uuid.UUID(connection_id), uuid.UUID(record_id), payload
            )
        )
    except Exception as exc:
        logger.exception("whatsapp inbound task failed for message %s", record_id)
        raise self.retry(exc=exc) from exc
