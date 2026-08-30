"""Background work for the knowledge base (spec §5.2.1, §5.2.3).

Each unit of work exists twice, deliberately:

* an **async function** taking a session — the real implementation, callable directly, which is what
  the tests exercise and what ``inline`` mode runs;
* a **Celery task** wrapping it — a thin shell that opens a session and calls the function.

Nothing but the shell knows about Celery, so the logic is testable without a broker and the retry
policy lives in one obvious place.

**Every task is safe to run twice.** ``task_acks_late`` means a task whose worker dies comes back,
and the sweep can enqueue a source a second time if a worker is slow. Re-extracting a source
overwrites the same row with the same result, and an unchanged record is skipped by fingerprint, so
a duplicate run costs time and nothing else.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src import configs
from src.core.queue import celery_app, enqueue, run_async
from src.modules.knowledge_base.domain.models import (
    KbSource,
    KnowledgeBase,
    SourceStatus,
    SourceType,
)
from src.modules.knowledge_base.internal import limits, redaction
from src.modules.knowledge_base.internal.connectors import ConnectorError, RestConnector
from src.modules.knowledge_base.internal.connectors.base import ConnectorRecord
from src.modules.knowledge_base.internal.extractors import (
    ExtractedContent,
    ExtractionError,
    Extractor,
    get_extractor,
    media_type_for,
)
from src.modules.knowledge_base.internal.fetching import fetch

logger = logging.getLogger("api.knowledge_base.tasks")

# Where a source's original bytes are kept between the upload request and the worker picking it up.
# JSONB on the row would be wrong for a 10 MB file; object storage is the real answer and is Phase
# 13's to introduce. Until then an uploaded file is handed to the task directly in inline mode and
# staged on the row in redis mode — see `stage_upload`.
UPLOAD_STAGE_KEY = "stagedUpload"

# Named here so the connector this module builds can be substituted in tests without reaching into
# the connectors package — the seam is the task's, not the connector's.
__all__ = [
    "UPLOAD_STAGE_KEY",
    "RestConnector",
    "due_sources",
    "extract_source",
    "extract_source_task",
    "needs_attention",
    "sweep_due_sources",
    "sweep_due_sources_task",
    "sync_source",
    "sync_source_task",
]


# -- extraction -----------------------------------------------------------------------


async def extract_source(
    session: AsyncSession,
    source_id: uuid.UUID,
    content: ExtractedContent | None = None,
    llm_extractor: Extractor | None = None,
) -> KbSource | None:
    """Extract one source and record the outcome on it.

    ``content`` is passed when the caller already holds the bytes — the inline path, where the
    upload request has them in memory. A worker re-derives them instead: it fetches a URL again, or
    reads the staged upload.

    Returns ``None`` when the source has been deleted since it was queued, which is a normal race
    rather than an error: a tenant may remove a source while it is still waiting.
    """
    source = await session.get(KbSource, source_id)
    if source is None:
        logger.info("source %s no longer exists; nothing to extract", source_id)
        return None

    await _mark(session, source, SourceStatus.PROCESSING)

    try:
        payload = content or await _content_for(source)
        extractor = get_extractor(payload.media_type, llm_extractor)
        result = await extractor.extract(payload)
    except (ExtractionError, ConnectorError) as exc:
        return await _fail(session, source, str(exc))
    except SoftTimeLimitExceeded:
        # Raised inside the task by Celery's soft limit, so the source can record why it stopped
        # rather than being left in `processing` for ever by the hard kill.
        return await _fail(session, source, "Extraction took too long and was stopped.")

    now = datetime.now(UTC)
    text, removed = await _redacted(session, source, result.text)

    source.status = SourceStatus.READY
    source.extracted_text = text
    source.error_detail = None
    source.config_json = {
        **source.config_json,
        **result.metadata,
        **({"redacted": removed} if removed else {}),
    }
    source.last_synced_at = now
    source.source_updated_at = now
    source.consecutive_failures = 0
    _schedule_next(source)
    await session.flush()
    return source


async def _redacted(
    session: AsyncSession, source: KbSource, text: str | None
) -> tuple[str | None, dict[str, int]]:
    """Apply the knowledge base's redaction policy before the text is stored (spec §5.7).

    The flag lives on the knowledge base rather than the source, so the worker has to load it. One
    extra read per extraction, on a path that has just paid for a model call or an HTTP fetch —
    which is the cheapest possible place to put it, and far cheaper than a policy that could be
    forgotten on one of the three code paths that store extracted text.
    """
    knowledge_base = await session.get(KnowledgeBase, source.kb_id)
    return redaction.apply(text, bool(knowledge_base and knowledge_base.redact_pii))


async def _content_for(source: KbSource) -> ExtractedContent:
    """Re-derive a source's bytes in a worker that did not receive them."""
    if source.type is SourceType.URL:
        url = str(source.config_json.get("url") or "")
        page = await fetch(url, max_bytes=limits.max_source_bytes())
        return ExtractedContent(
            data=page.body,
            media_type=media_type_for(page.url, page.media_type),
            filename=page.url,
        )

    staged = source.config_json.get(UPLOAD_STAGE_KEY)
    if isinstance(staged, str):
        import base64

        return ExtractedContent(
            data=base64.b64decode(staged),
            media_type=str(source.config_json.get("mediaType") or "text/plain"),
            filename=str(source.config_json.get("filename") or "upload"),
        )

    raise ExtractionError(
        "The uploaded content is no longer available. Please upload the file again."
    )


# -- connector sync ---------------------------------------------------------------------


async def sync_source(session: AsyncSession, source_id: uuid.UUID) -> KbSource | None:
    """Pull an API source and re-index it if anything changed (spec §5.2.1 Pattern B).

    Incremental by fingerprint: if every record comes back with the version it had last time, the
    stored text is left alone and only ``last_synced_at`` moves. That is the difference between a
    nightly sync costing nothing and it rewriting a tenant's whole catalogue every night.
    """
    source = await session.get(KbSource, source_id)
    if source is None:
        return None
    if source.type is not SourceType.API_INDEXED:
        logger.warning("source %s is not an API source; refusing to sync it", source_id)
        return source

    await _mark(session, source, SourceStatus.PROCESSING)

    try:
        connector = RestConnector(dict(source.config_json.get("connector") or {}))
        result = await connector.fetch()
    except ConnectorError as exc:
        return await _fail(session, source, str(exc))
    except SoftTimeLimitExceeded:
        return await _fail(session, source, "The sync took too long and was stopped.")

    if not result.records:
        return await _fail(
            session, source, "The endpoint returned no records that match the field mapping."
        )

    fingerprint = _fingerprint_of(result.records)
    now = datetime.now(UTC)

    if fingerprint == source.sync_cursor:
        # Nothing changed. The pull still happened, so `last_synced_at` moves and the failure
        # counter resets, but the text is untouched and no re-extraction is paid for.
        source.status = SourceStatus.READY
        source.last_synced_at = now
        source.consecutive_failures = 0
        source.error_detail = None
        _schedule_next(source)
        await session.flush()
        logger.info("source %s unchanged since last sync; skipped", source_id)
        return source

    joined = "\n".join(record.text for record in result.records)
    redacted, removed = await _redacted(session, source, joined)
    text = redacted or ""

    source.status = SourceStatus.READY
    source.extracted_text = text
    source.error_detail = None
    source.sync_cursor = fingerprint
    # Measured on the stored text, not on what the endpoint returned: the storage quota is about
    # what the platform is keeping, and redaction changes that.
    source.byte_size = len(text.encode("utf-8"))
    source.consecutive_failures = 0
    source.last_synced_at = now
    source.source_updated_at = now
    source.config_json = {
        **source.config_json,
        "format": "api",
        "records": len(result.records),
        "pagesFetched": result.pages_fetched,
        "truncated": result.truncated,
        "characters": len(text),
        **({"redacted": removed} if removed else {}),
    }
    _schedule_next(source)
    await session.flush()
    return source


def _fingerprint_of(records: list[ConnectorRecord]) -> str:
    """One value standing for the whole pull, so "did anything change?" is a string comparison."""
    import hashlib

    digest = hashlib.sha256()
    for record in records:
        digest.update(record.external_id.encode("utf-8"))
        digest.update(record.fingerprint().encode("utf-8"))
    return digest.hexdigest()


# -- the sweep --------------------------------------------------------------------------


async def due_sources(session: AsyncSession, limit: int = 200) -> list[KbSource]:
    """Sources whose next sync has come around.

    Read across every tenant, which is the one place that is correct: this runs on a schedule with
    no request and no tenant, and it dispatches per-source work that is itself scoped by the source
    row it was given.
    """
    now = datetime.now(UTC)
    query = (
        select(KbSource)
        .where(
            KbSource.type == SourceType.API_INDEXED,
            KbSource.sync_interval_minutes > 0,
            KbSource.next_sync_at.is_not(None),
            KbSource.next_sync_at <= now,
        )
        .order_by(KbSource.next_sync_at)
        .limit(limit)
    )
    return list((await session.execute(query)).scalars().all())


async def sweep_due_sources(session: AsyncSession) -> int:
    """Enqueue every source that is due, and return how many.

    Each source's ``next_sync_at`` is pushed forward *before* its task is enqueued, so a slow worker
    cannot cause the next sweep to enqueue the same source again a minute later.
    """
    sources = await due_sources(session)
    for source in sources:
        _schedule_next(source)
        enqueue(sync_source_task, str(source.id))
    await session.flush()

    if sources:
        logger.info("swept %d source(s) due for sync", len(sources))
    return len(sources)


# -- shared bookkeeping ------------------------------------------------------------------


def _schedule_next(source: KbSource) -> None:
    interval = source.sync_interval_minutes
    source.next_sync_at = datetime.now(UTC) + timedelta(minutes=interval) if interval > 0 else None


async def _mark(session: AsyncSession, source: KbSource, status: SourceStatus) -> None:
    source.status = status
    await session.flush()


def needs_attention(source: KbSource) -> bool:
    """Whether this source has failed often enough to be reported as broken (spec §5.2.1).

    A decision rather than a log line, so the same rule answers "should we alert?", "what should
    the console show as broken?" and, in Phase 12, "what belongs on the error dashboard?" — one
    threshold, one definition.
    """
    threshold: int = configs.SYNC_FAILURE_ALERT_THRESHOLD
    return source.consecutive_failures >= threshold


async def _fail(session: AsyncSession, source: KbSource, detail: str) -> KbSource:
    """Record a failure on the source, and raise the alarm once it stops looking like a blip."""
    source.status = SourceStatus.FAILED
    source.error_detail = detail
    source.consecutive_failures += 1
    source.last_synced_at = datetime.now(UTC)
    # A failed source stores nothing usable, so it stops counting against the storage quota.
    source.byte_size = 0
    _schedule_next(source)
    await session.flush()

    if needs_attention(source):
        logger.error(
            "source %s has failed %d consecutive times and needs attention: %s",
            source.id,
            source.consecutive_failures,
            detail,
        )
    else:
        logger.warning("source %s failed: %s", source.id, detail)
    return source


# -- Celery shells -------------------------------------------------------------------------


@celery_app.task(
    name="knowledge_base.extract_source",
    bind=True,
    max_retries=configs.QUEUE_MAX_RETRIES,
    default_retry_delay=configs.QUEUE_RETRY_BACKOFF_SECONDS,
)
def extract_source_task(self: Any, source_id: str) -> None:
    """Extract a source in a worker.

    Only *unexpected* failures retry. An unreadable document is not a transient problem — it has
    already been recorded on the source as a readable error, and retrying it three times would
    just spend three times as long reaching the same answer.
    """
    try:
        run_async(lambda session: extract_source(session, uuid.UUID(source_id)))
    except Exception as exc:
        logger.exception("extraction task failed for source %s", source_id)
        raise self.retry(exc=exc) from exc


@celery_app.task(
    name="knowledge_base.sync_source",
    bind=True,
    max_retries=configs.QUEUE_MAX_RETRIES,
    default_retry_delay=configs.QUEUE_RETRY_BACKOFF_SECONDS,
)
def sync_source_task(self: Any, source_id: str) -> None:
    try:
        run_async(lambda session: sync_source(session, uuid.UUID(source_id)))
    except Exception as exc:
        logger.exception("sync task failed for source %s", source_id)
        raise self.retry(exc=exc) from exc


@celery_app.task(name="knowledge_base.sweep_due_sources")
def sweep_due_sources_task() -> int:
    """Beat's entry point. Runs every minute and enqueues whatever is due.

    Not retried: if a sweep fails, the next one is sixty seconds away and will pick up the same
    sources, because nothing was marked as done.
    """
    return run_async(sweep_due_sources)
