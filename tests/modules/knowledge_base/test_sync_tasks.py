"""Background ingestion, scheduled sync and failure alerting (spec §5.2.1).

The phase's bar lives here: ingestion no longer runs in the request, a connector pulls and indexes a
paginated source on a schedule, an unchanged record is skipped on re-sync, and a source that keeps
breaking is reported rather than failing silently.

Tasks are exercised as the plain async functions they are. The Celery shell around them is three
lines of session plumbing; testing through a broker would test Celery, not this code.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.knowledge_base.domain.models import KbSource, SourceStatus, SourceType
from src.modules.knowledge_base.domain.services import KnowledgeBaseService
from src.modules.knowledge_base.internal import tasks
from src.modules.tenants.domain.models import Tenant

ENDPOINT = "http://127.0.0.1/api/products"

CONNECTOR: dict[str, Any] = {
    "url": ENDPOINT,
    "contentFields": ["sku", "name", "price"],
    "idField": "sku",
    "versionField": "updated_at",
}


@pytest.fixture(autouse=True)
def allow_loopback(config_override: Callable[..., None]) -> None:
    config_override(KB_ALLOW_PRIVATE_URLS="true")


@pytest.fixture
async def tenant(make_tenant: Callable[..., Coroutine[Any, Any, Tenant]]) -> Tenant:
    return await make_tenant(name="Nash Paints")


@pytest.fixture
def service(session: AsyncSession, tenant: Tenant) -> KnowledgeBaseService:
    return KnowledgeBaseService(session, tenant.id)


def product(sku: str, price: float = 45.99, updated: str = "2026-01-01") -> dict[str, Any]:
    return {"sku": sku, "name": "Matt white", "price": price, "updated_at": updated}


def patch_connector(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Give the connector a mock transport wherever the task builds one."""
    real = tasks.RestConnector

    def build(config: dict[str, Any], client: httpx.AsyncClient | None = None) -> Any:
        return real(config, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(tasks, "RestConnector", build)


# -- ingestion is off the request path -------------------------------------------------


async def test_an_upload_returns_before_the_work_is_done(
    service: KnowledgeBaseService,
    config_override: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The phase's bar. In redis mode the request stores the row and hands off — nothing is
    extracted before it returns."""
    config_override(QUEUE_MODE="redis")
    enqueued: list[tuple[Any, tuple[Any, ...]]] = []
    monkeypatch.setattr("src.core.queue.enqueue", lambda task, *args: enqueued.append((task, args)))
    monkeypatch.setattr(
        "src.modules.knowledge_base.domain.services.queue.enqueue",
        lambda task, *args: enqueued.append((task, args)),
    )
    knowledge_base = await service.create(name="Policies")

    source = await service.add_file_source(
        knowledge_base.id, filename="prices.txt", data=b"Matt white is $45."
    )

    assert source.status is SourceStatus.PENDING
    assert source.extracted_text is None
    assert enqueued, "the extraction was handed to a worker"


async def test_the_worker_then_advances_the_source_to_ready(
    session: AsyncSession,
    service: KnowledgeBaseService,
    config_override: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_override(QUEUE_MODE="redis")
    monkeypatch.setattr(
        "src.modules.knowledge_base.domain.services.queue.enqueue", lambda task, *args: None
    )
    knowledge_base = await service.create(name="Policies")
    source = await service.add_file_source(
        knowledge_base.id, filename="prices.txt", data=b"Matt white is $45."
    )
    assert source.status is SourceStatus.PENDING

    extracted = await tasks.extract_source(session, source.id)

    assert extracted is not None
    assert extracted.status is SourceStatus.READY
    assert extracted.extracted_text == "Matt white is $45."


async def test_a_worker_reads_the_staged_upload(
    session: AsyncSession,
    service: KnowledgeBaseService,
    config_override: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker did not receive the bytes, so it has to find them on the row."""
    config_override(QUEUE_MODE="redis")
    monkeypatch.setattr(
        "src.modules.knowledge_base.domain.services.queue.enqueue", lambda task, *args: None
    )
    knowledge_base = await service.create(name="Policies")
    source = await service.add_file_source(
        knowledge_base.id, filename="notes.md", data=b"# Notes\n\nMix well."
    )

    assert tasks.UPLOAD_STAGE_KEY in source.config_json
    extracted = await tasks.extract_source(session, source.id)

    assert extracted is not None
    assert "# Notes" in (extracted.extracted_text or "")


async def test_extracting_a_deleted_source_is_not_an_error(
    session: AsyncSession, service: KnowledgeBaseService
) -> None:
    """A tenant may delete a source while it is still queued. That is a race, not a failure."""
    assert await tasks.extract_source(session, uuid.uuid4()) is None


# -- Pattern B: pull and index -----------------------------------------------------------


async def test_a_paginated_api_source_is_pulled_and_indexed(
    service: KnowledgeBaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = {1: [product("SKU1"), product("SKU2")], 2: [product("SKU3")], 3: []}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages.get(int(request.url.params.get("page", 1)), []))

    patch_connector(monkeypatch, handler)
    knowledge_base = await service.create(name="Catalogue")

    source = await service.add_api_source(
        knowledge_base.id,
        name="Products",
        connector={**CONNECTOR, "pagination": "page"},
        sync_interval_minutes=60,
    )

    assert source.status is SourceStatus.READY
    assert source.type is SourceType.API_INDEXED
    assert "SKU1" in (source.extracted_text or "")
    assert "SKU3" in (source.extracted_text or "")
    assert source.config_json["records"] == 3
    assert source.next_sync_at is not None


async def test_an_unchanged_source_is_skipped_on_re_sync(
    session: AsyncSession, service: KnowledgeBaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The phase's bar. A nightly sync of an unchanged catalogue must cost nothing."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[product("SKU1")])

    patch_connector(monkeypatch, handler)
    knowledge_base = await service.create(name="Catalogue")
    source = await service.add_api_source(
        knowledge_base.id, name="Products", connector=CONNECTOR, sync_interval_minutes=60
    )
    first_text, cursor = source.extracted_text, source.sync_cursor
    await service.sources.update(source, extracted_text="tampered, to prove it is not rewritten")

    resynced = await tasks.sync_source(session, source.id)

    assert resynced is not None
    assert resynced.sync_cursor == cursor
    assert resynced.extracted_text == "tampered, to prove it is not rewritten"
    assert resynced.status is SourceStatus.READY
    assert first_text is not None


async def test_a_changed_record_is_re_indexed(
    session: AsyncSession, service: KnowledgeBaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = {"value": "2026-01-01"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[product("SKU1", price=52.00, updated=version["value"])])

    patch_connector(monkeypatch, handler)
    knowledge_base = await service.create(name="Catalogue")
    source = await service.add_api_source(
        knowledge_base.id, name="Products", connector=CONNECTOR, sync_interval_minutes=60
    )
    before = source.sync_cursor

    version["value"] = "2026-02-01"
    resynced = await tasks.sync_source(session, source.id)

    assert resynced is not None
    assert resynced.sync_cursor != before
    assert "52.0" in (resynced.extracted_text or "")


async def test_incremental_sync_works_without_a_version_field(
    session: AsyncSession, service: KnowledgeBaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A content hash stands in, so an API with no version field still skips unchanged pulls."""
    price = {"value": 45.99}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[product("SKU1", price=price["value"])])

    patch_connector(monkeypatch, handler)
    knowledge_base = await service.create(name="Catalogue")
    source = await service.add_api_source(
        knowledge_base.id,
        name="Products",
        connector={**CONNECTOR, "versionField": None},
        sync_interval_minutes=60,
    )
    unchanged = await tasks.sync_source(session, source.id)
    assert unchanged is not None
    cursor = unchanged.sync_cursor

    price["value"] = 99.00
    changed = await tasks.sync_source(session, source.id)

    assert changed is not None
    assert changed.sync_cursor != cursor


# -- failure and alerting ------------------------------------------------------------------


async def test_expired_credentials_leave_a_readable_error_on_the_source(
    session: AsyncSession, service: KnowledgeBaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    patch_connector(monkeypatch, handler)
    knowledge_base = await service.create(name="Catalogue")

    source = await service.add_api_source(
        knowledge_base.id, name="Products", connector=CONNECTOR, sync_interval_minutes=60
    )

    assert source.status is SourceStatus.FAILED
    assert "expired" in (source.error_detail or "")
    assert source.consecutive_failures == 1
    assert session is not None


async def test_repeated_failures_are_counted_and_alerted(
    session: AsyncSession,
    service: KnowledgeBaseService,
    monkeypatch: pytest.MonkeyPatch,
    config_override: Callable[..., None],
) -> None:
    """A source that keeps breaking must be reported, not left to rot silently (§5.2.1)."""
    config_override(KB_ALLOW_PRIVATE_URLS="true", SYNC_FAILURE_ALERT_THRESHOLD=2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    patch_connector(monkeypatch, handler)
    knowledge_base = await service.create(name="Catalogue")
    source = await service.add_api_source(
        knowledge_base.id, name="Products", connector=CONNECTOR, sync_interval_minutes=60
    )

    assert tasks.needs_attention(source) is False, "one failure is a blip, not an alarm"

    second = await tasks.sync_source(session, source.id)

    assert second is not None
    assert second.consecutive_failures == 2
    assert second.status is SourceStatus.FAILED
    assert second.error_detail
    assert tasks.needs_attention(second) is True, "a source this broken must be reported"


async def test_a_successful_sync_clears_the_failure_count(
    session: AsyncSession, service: KnowledgeBaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = [httpx.Response(500), httpx.Response(200, json=[product("SKU1")])]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0) if responses else httpx.Response(200, json=[product("SKU1")])

    patch_connector(monkeypatch, handler)
    knowledge_base = await service.create(name="Catalogue")
    source = await service.add_api_source(
        knowledge_base.id, name="Products", connector=CONNECTOR, sync_interval_minutes=60
    )
    assert source.consecutive_failures == 1

    recovered = await tasks.sync_source(session, source.id)

    assert recovered is not None
    assert recovered.consecutive_failures == 0
    assert recovered.status is SourceStatus.READY


async def test_a_failed_source_stores_nothing_against_the_quota(
    service: KnowledgeBaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    patch_connector(monkeypatch, handler)
    knowledge_base = await service.create(name="Catalogue")

    source = await service.add_api_source(knowledge_base.id, name="Products", connector=CONNECTOR)

    assert source.byte_size == 0


# -- the sweep -------------------------------------------------------------------------------


async def test_the_sweep_finds_only_sources_that_are_due(
    session: AsyncSession, service: KnowledgeBaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[product("SKU1")])

    patch_connector(monkeypatch, handler)
    knowledge_base = await service.create(name="Catalogue")

    due = await service.add_api_source(
        knowledge_base.id, name="Due", connector=CONNECTOR, sync_interval_minutes=60
    )
    not_due = await service.add_api_source(
        knowledge_base.id, name="Not due", connector=CONNECTOR, sync_interval_minutes=60
    )
    unscheduled = await service.add_api_source(
        knowledge_base.id, name="Manual only", connector=CONNECTOR, sync_interval_minutes=0
    )
    await service.sources.update(due, next_sync_at=datetime.now(UTC) - timedelta(minutes=1))

    found = await tasks.due_sources(session)

    identifiers = {source.id for source in found}
    assert due.id in identifiers
    assert not_due.id not in identifiers
    assert unscheduled.id not in identifiers


async def test_the_sweep_pushes_the_next_run_forward_before_enqueueing(
    session: AsyncSession, service: KnowledgeBaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise a slow worker means the next sweep enqueues the same source again."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[product("SKU1")])

    patch_connector(monkeypatch, handler)
    monkeypatch.setattr("src.modules.knowledge_base.internal.tasks.enqueue", lambda *a, **k: None)
    knowledge_base = await service.create(name="Catalogue")
    source = await service.add_api_source(
        knowledge_base.id, name="Due", connector=CONNECTOR, sync_interval_minutes=60
    )
    await service.sources.update(source, next_sync_at=datetime.now(UTC) - timedelta(minutes=1))

    swept = await tasks.sweep_due_sources(session)

    assert swept == 1
    assert await tasks.due_sources(session) == []


async def test_a_file_source_is_never_swept(
    session: AsyncSession, service: KnowledgeBaseService
) -> None:
    """A file has no origin to re-read; scheduling one would be a permanent no-op."""
    knowledge_base = await service.create(name="Policies")
    source = await service.add_file_source(
        knowledge_base.id, filename="a.txt", data=b"Matt white is $45."
    )
    await service.sources.update(
        source, sync_interval_minutes=60, next_sync_at=datetime.now(UTC) - timedelta(minutes=1)
    )

    assert await tasks.due_sources(session) == []


async def test_syncing_a_file_source_is_refused_by_the_task(
    session: AsyncSession, service: KnowledgeBaseService
) -> None:
    knowledge_base = await service.create(name="Policies")
    source = await service.add_file_source(
        knowledge_base.id, filename="a.txt", data=b"Matt white is $45."
    )

    result = await tasks.sync_source(session, source.id)

    assert result is not None
    assert result.status is SourceStatus.READY, "the source is left exactly as it was"


# -- schedule management -----------------------------------------------------------------------


async def test_a_schedule_can_be_set_and_cleared(
    service: KnowledgeBaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[product("SKU1")])

    patch_connector(monkeypatch, handler)
    knowledge_base = await service.create(name="Catalogue")
    source = await service.add_api_source(knowledge_base.id, name="Products", connector=CONNECTOR)

    scheduled = await service.set_sync_schedule(knowledge_base.id, source.id, 60)
    assert scheduled.sync_interval_minutes == 60
    assert scheduled.next_sync_at is not None

    cleared = await service.set_sync_schedule(knowledge_base.id, source.id, 0)
    assert cleared.sync_interval_minutes == 0
    assert cleared.next_sync_at is None


async def test_too_short_an_interval_is_refused(
    service: KnowledgeBaseService,
    monkeypatch: pytest.MonkeyPatch,
    config_override: Callable[..., None],
) -> None:
    """The floor protects the tenant's own supplier as much as us."""
    config_override(KB_ALLOW_PRIVATE_URLS="true", SYNC_MIN_INTERVAL_MINUTES=15)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[product("SKU1")])

    patch_connector(monkeypatch, handler)
    knowledge_base = await service.create(name="Catalogue")
    source = await service.add_api_source(knowledge_base.id, name="Products", connector=CONNECTOR)

    from src.shared.exceptions import ValidationException

    with pytest.raises(ValidationException) as caught:
        await service.set_sync_schedule(knowledge_base.id, source.id, 5)

    assert caught.value.code == "KB_SYNC_INTERVAL_TOO_SHORT"


async def test_a_manual_entry_cannot_be_scheduled_or_synced(
    service: KnowledgeBaseService,
) -> None:
    knowledge_base = await service.create(name="Policies")
    source = await service.add_manual_source(knowledge_base.id, title="Q", body="A")

    from src.shared.exceptions import ValidationException

    with pytest.raises(ValidationException):
        await service.sync_now(knowledge_base.id, source.id)
    with pytest.raises(ValidationException):
        await service.set_sync_schedule(knowledge_base.id, source.id, 60)


async def test_manual_sync_re_reads_a_url_source(
    service: KnowledgeBaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manual half of §5.2's re-sync controls."""
    body = {"html": b"<html><body><main><h1>First</h1></main></body></html>"}

    async def fake_fetch(url: str, max_bytes: int, client: object | None = None) -> Any:
        from src.modules.knowledge_base.internal.fetching import FetchedPage

        return FetchedPage(url=url, body=body["html"], media_type="text/html")

    monkeypatch.setattr("src.modules.knowledge_base.internal.tasks.fetch", fake_fetch)
    knowledge_base = await service.create(name="Policies")
    source = await service.add_url_source(knowledge_base.id, url="https://example.com/policy")
    assert "First" in (source.extracted_text or "")

    body["html"] = b"<html><body><main><h1>Second</h1></main></body></html>"
    resynced = await service.sync_now(knowledge_base.id, source.id)

    assert "Second" in (resynced.extracted_text or "")


async def test_a_source_kept_by_the_sweep_query_is_tenant_agnostic(
    session: AsyncSession, service: KnowledgeBaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep runs with no request and no tenant, so its read is deliberately unscoped —
    but every source it hands on is acted upon through the row it was given."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[product("SKU1")])

    patch_connector(monkeypatch, handler)
    knowledge_base = await service.create(name="Catalogue")
    source = await service.add_api_source(
        knowledge_base.id, name="Products", connector=CONNECTOR, sync_interval_minutes=60
    )
    await service.sources.update(source, next_sync_at=datetime.now(UTC) - timedelta(minutes=1))

    found = await tasks.due_sources(session)

    assert [item.id for item in found] == [source.id]
    assert all(isinstance(item, KbSource) for item in found)
