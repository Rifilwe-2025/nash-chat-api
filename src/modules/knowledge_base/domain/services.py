"""Knowledge base business logic: CRUD, attachment, and ingestion (spec §5.2).

Two things are worth knowing before reading further.

**Extraction happens off the request path.** A source is written as ``PENDING`` and a worker
advances it to ``READY`` or ``FAILED`` (Phase 9). The upload returns as soon as the row exists, so a
40 MB PDF read by a model no longer holds a request open for a minute. In ``inline`` queue mode the
work still runs in the caller — that is a local-development convenience, not how a deployment runs.

**Extraction failure is not an error response.** A password-protected PDF, a 404 URL, or a CSV with
no rows produces a source in ``FAILED`` status carrying a readable ``error_detail`` — the upload
itself succeeds. A bad document must be inspectable in the API, not a 500. Limit and type
violations are the exception, since they are rejected before anything is stored.

**Cross-module access goes service → service.** Attaching a knowledge base to an agent asks
``AgentService`` for the agent; this module never touches the agents module's repositories or
models. That also means the agent lookup is scoped by the agents module's own rules, so an agent id
from another tenant is simply not found.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src import configs
from src.core import queue
from src.modules.agents.domain.services import AgentService
from src.modules.knowledge_base.domain.models import (
    AgentKbLink,
    KbSource,
    KnowledgeBase,
    RetrievalTier,
    SourceStatus,
    SourceType,
)
from src.modules.knowledge_base.domain.repositories import (
    AgentKbLinkRepository,
    KbSourceRepository,
    KnowledgeBaseRepository,
)
from src.modules.knowledge_base.internal import limits, tasks
from src.modules.knowledge_base.internal.extractors import (
    ExtractedContent,
    ExtractionError,
    ExtractionResult,
    Extractor,
    get_extractor,
    media_type_for,
)
from src.modules.knowledge_base.internal.retrieval import (
    NoContextReason,
    RetrievalResult,
    TierDecision,
    choose_tier,
    retrieve_direct,
    retrieve_keyword,
)
from src.shared.database.pagination import Page, PageRequest
from src.shared.exceptions import ConflictException, NotFoundException, ValidationException

logger = logging.getLogger("api.knowledge_base")

MANUAL_MEDIA_TYPE = "text/markdown"


class KnowledgeBaseService:
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        llm_extractor: Extractor | None = None,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.knowledge_bases = KnowledgeBaseRepository(session, tenant_id)
        self.sources = KbSourceRepository(session, tenant_id)
        self.links = AgentKbLinkRepository(session)
        self.agents = AgentService(session, tenant_id)
        # Injected so the PDF/image path can be exercised without calling a provider.
        self._llm_extractor = llm_extractor

    # -- knowledge bases -----------------------------------------------------

    async def get(self, kb_id: uuid.UUID) -> KnowledgeBase:
        knowledge_base = await self.knowledge_bases.get(kb_id)
        if knowledge_base is None:
            raise NotFoundException("Knowledge base does not exist.", code="KB_NOT_FOUND")
        return knowledge_base

    async def list_knowledge_bases(
        self, page: PageRequest, agent_id: uuid.UUID | None = None
    ) -> Page[KnowledgeBase]:
        if agent_id is None:
            return await self.knowledge_bases.list(page)
        await self.agents.get(agent_id)  # 404s a foreign agent before listing anything
        return await self.knowledge_bases.list_for_agent(agent_id, page)

    async def create(
        self,
        name: str,
        description: str = "",
        retrieval_tier: RetrievalTier = RetrievalTier.AUTO,
    ) -> KnowledgeBase:
        await self._require_unique_name(name)
        return await self.knowledge_bases.add(
            KnowledgeBase(name=name, description=description, retrieval_tier=retrieval_tier)
        )

    async def update(self, kb_id: uuid.UUID, changes: dict[str, Any]) -> KnowledgeBase:
        knowledge_base = await self.get(kb_id)

        applied = {key: value for key, value in changes.items() if value is not None}
        if "name" in applied:
            await self._require_unique_name(applied["name"], exclude_id=knowledge_base.id)
        if not applied:
            return knowledge_base
        return await self.knowledge_bases.update(knowledge_base, **applied)

    async def delete(self, kb_id: uuid.UUID) -> None:
        """Removes the knowledge base, its sources, and its agent links.

        The links go with it by cascade rather than blocking the delete: a knowledge base that is
        still attached is the normal case, and an agent losing one is a configuration change, not a
        corruption.
        """
        await self.knowledge_bases.delete(await self.get(kb_id))

    async def stats(self, kb_id: uuid.UUID) -> tuple[int, int]:
        """``(source count, attached agent count)`` for the detail response."""
        return (
            await self.sources.count_for_kb(kb_id),
            await self.links.count_for_kb(kb_id),
        )

    async def storage_used(self) -> int:
        return await self.sources.total_bytes()

    # -- agent attachment ----------------------------------------------------

    async def attach(self, kb_id: uuid.UUID, agent_id: uuid.UUID) -> None:
        """Make a knowledge base available to an agent. Attaching twice is a no-op.

        Both ends are resolved through their own scoped paths first, so a link can only ever join
        two objects this tenant owns (spec §5.7).
        """
        knowledge_base = await self.get(kb_id)
        agent = await self.agents.get(agent_id)

        if await self.links.get_link(agent.id, knowledge_base.id) is not None:
            return
        await self.links.add(AgentKbLink(agent_id=agent.id, kb_id=knowledge_base.id))

    async def detach(self, kb_id: uuid.UUID, agent_id: uuid.UUID) -> None:
        knowledge_base = await self.get(kb_id)
        agent = await self.agents.get(agent_id)

        link = await self.links.get_link(agent.id, knowledge_base.id)
        if link is None:
            raise NotFoundException(
                "That knowledge base is not attached to this agent.", code="KB_LINK_NOT_FOUND"
            )
        await self.links.delete(link)

    async def attached_agent_ids(self, kb_id: uuid.UUID) -> list[uuid.UUID]:
        await self.get(kb_id)
        return await self.links.agent_ids_for_kb(kb_id)

    # -- retrieval -----------------------------------------------------------

    async def retrieve(
        self,
        query: str,
        kb_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        model: str | None = None,
    ) -> RetrievalResult:
        """The one way to get knowledge out of this module (spec §5.2.2).

        Callers pass a question and either a knowledge base or an agent, and get passages back.
        They never choose a tier, never see a ``tsquery``, and will not have to change when Tier 3
        arrives in v2 — which is the point of routing here rather than at the call site.

        Give ``agent_id`` to search everything that agent draws on: an agent may have several
        knowledge bases attached, and its answer should be able to come from any of them.
        """
        kb_ids, configured = await self._retrieval_scope(kb_id, agent_id)
        if not kb_ids:
            return RetrievalResult(
                tier=RetrievalTier.DIRECT,
                no_context_reason=NoContextReason.EMPTY_KNOWLEDGE_BASE,
            )

        total = await self.sources.total_characters(kb_ids)
        decision = choose_tier(configured, total, model=model)

        if decision.tier is RetrievalTier.DIRECT:
            return retrieve_direct(
                await self.sources.ready_for_kbs(kb_ids),
                considered_characters=decision.considered_characters,
                budget_characters=decision.budget_characters,
            )

        matches = await self.sources.search(
            kb_ids, query, limit=configs.KNOWLEDGE_BASE_KEYWORD_TOP_N
        )
        return retrieve_keyword(
            matches,
            min_rank=configs.KNOWLEDGE_BASE_KEYWORD_MIN_RANK,
            considered_characters=decision.considered_characters,
            budget_characters=decision.budget_characters,
        )

    async def explain_retrieval(
        self,
        query: str,
        kb_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        model: str | None = None,
    ) -> tuple[TierDecision, RetrievalResult]:
        """The same retrieval, plus why that tier was chosen — the debugging surface (§5.2).

        Deliberately delegates to :meth:`retrieve` rather than reimplementing the routing, at the
        cost of resolving the scope twice. An explain endpoint that could disagree with the real
        retrieval would be worse than useless, and this is a debugging path, not a hot one.
        """
        kb_ids, configured = await self._retrieval_scope(kb_id, agent_id)
        total = await self.sources.total_characters(kb_ids)
        decision = choose_tier(configured, total, model=model)
        return decision, await self.retrieve(query, kb_id=kb_id, agent_id=agent_id, model=model)

    async def _retrieval_scope(
        self, kb_id: uuid.UUID | None, agent_id: uuid.UUID | None
    ) -> tuple[list[uuid.UUID], RetrievalTier]:
        """Which knowledge bases to search, and the tier they are configured for.

        Both paths go through a scoped read first, so a retrieval can only ever reach this tenant's
        knowledge (spec §5.7).
        """
        if (kb_id is None) == (agent_id is None):
            raise ValidationException(
                "Provide exactly one of a knowledge base or an agent to retrieve from.",
                code="KB_RETRIEVAL_SCOPE_REQUIRED",
            )

        if kb_id is not None:
            knowledge_base = await self.get(kb_id)
            return [knowledge_base.id], knowledge_base.retrieval_tier

        assert agent_id is not None
        await self.agents.get(agent_id)
        attached = await self.knowledge_bases.all_for_agent(agent_id)
        # An agent's knowledge bases may disagree about tier. The most restrictive setting wins:
        # if any one of them is too large to inject, or is pinned to search, injecting the others
        # whole alongside a search result would be a muddle.
        tiers = {knowledge_base.retrieval_tier for knowledge_base in attached}
        if RetrievalTier.KEYWORD in tiers:
            configured = RetrievalTier.KEYWORD
        elif tiers == {RetrievalTier.DIRECT}:
            configured = RetrievalTier.DIRECT
        else:
            configured = RetrievalTier.AUTO
        return [knowledge_base.id for knowledge_base in attached], configured

    # -- sources -------------------------------------------------------------

    async def get_source(self, kb_id: uuid.UUID, source_id: uuid.UUID) -> KbSource:
        await self.get(kb_id)
        source = await self.sources.get(source_id)
        if source is None or source.kb_id != kb_id:
            raise NotFoundException("Source does not exist.", code="KB_SOURCE_NOT_FOUND")
        return source

    async def list_sources(self, kb_id: uuid.UUID, page: PageRequest) -> Page[KbSource]:
        await self.get(kb_id)
        return await self.sources.list_for_kb(kb_id, page)

    async def delete_source(self, kb_id: uuid.UUID, source_id: uuid.UUID) -> None:
        await self.sources.delete(await self.get_source(kb_id, source_id))

    async def add_file_source(
        self,
        kb_id: uuid.UUID,
        filename: str,
        data: bytes,
        declared_media_type: str | None = None,
        name: str | None = None,
    ) -> KbSource:
        """Ingest an uploaded file: txt/md, docx, csv, html, pdf, or an image (spec §5.2.3)."""
        knowledge_base = await self.get(kb_id)
        media_type = self._resolve_media_type(filename, declared_media_type)
        await self._assert_within_limits(len(data))

        return await self._ingest(
            knowledge_base,
            name=name or filename,
            source_type=SourceType.FILE,
            byte_size=len(data),
            config={"filename": filename, "mediaType": media_type},
            content=ExtractedContent(data=data, media_type=media_type, filename=filename),
        )

    async def add_url_source(self, kb_id: uuid.UUID, url: str, name: str | None = None) -> KbSource:
        """Ingest a web page. The fetch happens in the worker, so a slow site does not block."""
        knowledge_base = await self.get(kb_id)

        source = await self.sources.add(
            KbSource(
                kb_id=knowledge_base.id,
                name=name or url,
                type=SourceType.URL,
                status=SourceStatus.PENDING,
                config_json={"url": url},
                byte_size=0,
            )
        )
        return await self._dispatch_extraction(source, content=None)

    async def add_api_source(
        self,
        kb_id: uuid.UUID,
        name: str,
        connector: dict[str, Any],
        sync_interval_minutes: int | None = None,
    ) -> KbSource:
        """Add a Pattern B API source and pull it for the first time (spec §5.2.1).

        The first pull happens now rather than at the next sweep: a tenant who has just configured a
        connector wants to know immediately whether the endpoint and credentials work, not in
        fifteen minutes.
        """
        knowledge_base = await self.get(kb_id)
        interval = self._validate_interval(sync_interval_minutes)

        source = await self.sources.add(
            KbSource(
                kb_id=knowledge_base.id,
                name=name,
                type=SourceType.API_INDEXED,
                status=SourceStatus.PENDING,
                config_json={"connector": connector},
                byte_size=0,
                sync_interval_minutes=interval,
            )
        )
        return await self._dispatch_sync(source)

    async def sync_now(self, kb_id: uuid.UUID, source_id: uuid.UUID) -> KbSource:
        """Re-pull a source on demand — the manual half of §5.2's re-sync controls."""
        source = await self.get_source(kb_id, source_id)

        if source.type is SourceType.API_INDEXED:
            return await self._dispatch_sync(source)
        if source.type is SourceType.URL:
            return await self._dispatch_extraction(source, content=None)
        raise ValidationException(
            "Only URL and API sources can be re-synced; a file or FAQ entry has no origin to "
            "re-read.",
            code="KB_SOURCE_NOT_SYNCABLE",
        )

    async def set_sync_schedule(
        self, kb_id: uuid.UUID, source_id: uuid.UUID, interval_minutes: int
    ) -> KbSource:
        """Change how often a source re-syncs. Zero stops it."""
        source = await self.get_source(kb_id, source_id)
        if source.type not in (SourceType.API_INDEXED, SourceType.URL):
            raise ValidationException(
                "Only URL and API sources can be scheduled.", code="KB_SOURCE_NOT_SYNCABLE"
            )

        interval = self._validate_interval(interval_minutes)
        return await self.sources.update(
            source,
            sync_interval_minutes=interval,
            next_sync_at=(
                datetime.now(UTC) + timedelta(minutes=interval) if interval > 0 else None
            ),
        )

    async def _dispatch_sync(self, source: KbSource) -> KbSource:
        if queue.is_inline():
            synced = await tasks.sync_source(self.session, source.id)
            return synced or source

        queue.enqueue(tasks.sync_source_task, str(source.id))
        return source

    def _validate_interval(self, minutes: int | None) -> int:
        """Zero disables scheduling; anything else must be at least the configured floor.

        The floor exists to protect the *tenant's* API as much as ours — a one-minute sync against
        someone's CMS is a way to get rate limited by your own supplier.
        """
        if minutes is None:
            default: int = configs.SYNC_DEFAULT_INTERVAL_MINUTES
            return default
        if minutes == 0:
            return 0

        floor: int = configs.SYNC_MIN_INTERVAL_MINUTES
        if minutes < floor:
            raise ValidationException(
                f"The sync interval must be 0 (never) or at least {floor} minutes.",
                code="KB_SYNC_INTERVAL_TOO_SHORT",
            )
        return minutes

    async def add_manual_source(self, kb_id: uuid.UUID, title: str, body: str) -> KbSource:
        """A FAQ entry typed into the builder — no extraction, the text is already text."""
        knowledge_base = await self.get(kb_id)

        text = f"# {title}\n\n{body}".strip()
        await self._assert_within_limits(len(text.encode("utf-8")))

        source = await self.sources.add(
            KbSource(
                kb_id=knowledge_base.id,
                name=title,
                type=SourceType.MANUAL,
                status=SourceStatus.PROCESSING,
                config_json={"title": title, "mediaType": MANUAL_MEDIA_TYPE},
                byte_size=len(text.encode("utf-8")),
            )
        )
        return await self._mark_ready(
            source,
            ExtractionResult(text=text, metadata={"format": "manual", "characters": len(text)}),
            byte_size=source.byte_size,
        )

    # -- internals -----------------------------------------------------------

    async def _ingest(
        self,
        knowledge_base: KnowledgeBase,
        *,
        name: str,
        source_type: SourceType,
        byte_size: int,
        config: dict[str, Any],
        content: ExtractedContent | None = None,
    ) -> KbSource:
        """Record the source, then hand its extraction to a worker.

        The row is written first so a failure has somewhere to be recorded and the tenant can see
        the source waiting. What happens next depends on the queue mode, and only the mode differs —
        both paths run exactly the same extraction code.
        """
        source = await self.sources.add(
            KbSource(
                kb_id=knowledge_base.id,
                name=name,
                type=source_type,
                status=SourceStatus.PENDING,
                config_json=self._staged(config, content),
                byte_size=byte_size,
            )
        )
        return await self._dispatch_extraction(source, content)

    async def _dispatch_extraction(
        self, source: KbSource, content: ExtractedContent | None
    ) -> KbSource:
        if queue.is_inline():
            extracted = await tasks.extract_source(
                self.session, source.id, content=content, llm_extractor=self._llm_extractor
            )
            return extracted or source

        queue.enqueue(tasks.extract_source_task, str(source.id))
        return source

    def _staged(self, config: dict[str, Any], content: ExtractedContent | None) -> dict[str, Any]:
        """Keep an uploaded file's bytes where the worker can find them.

        Base64 on the row is frankly a stopgap — object storage is the right home for a 10 MB
        upload and is Phase 13's to add. It is written only when a worker will need it, so the
        inline path never pays for it, and it is dropped as soon as extraction succeeds.
        """
        if content is None or queue.is_inline():
            return config
        import base64

        return {**config, tasks.UPLOAD_STAGE_KEY: base64.b64encode(content.data).decode("ascii")}

    async def _extract(self, content: ExtractedContent) -> ExtractionResult:
        extractor = get_extractor(content.media_type, self._llm_extractor)
        return await extractor.extract(content)

    async def _mark_ready(
        self,
        source: KbSource,
        result: ExtractionResult,
        *,
        byte_size: int,
        config: dict[str, Any] | None = None,
    ) -> KbSource:
        now = datetime.now(UTC)
        return await self.sources.update(
            source,
            status=SourceStatus.READY,
            extracted_text=result.text,
            error_detail=None,
            byte_size=byte_size,
            config_json={**(config or source.config_json), **result.metadata},
            last_synced_at=now,
            source_updated_at=now,
        )

    async def _mark_failed(self, source: KbSource, detail: str) -> KbSource:
        logger.warning("extraction failed for source %s: %s", source.id, detail)
        return await self.sources.update(
            source,
            status=SourceStatus.FAILED,
            error_detail=detail,
            extracted_text=None,
            byte_size=0,  # nothing usable was stored, so it does not count against the quota
            last_synced_at=datetime.now(UTC),
        )

    def _resolve_media_type(self, filename: str | None, declared: str | None) -> str:
        try:
            return media_type_for(filename, declared)
        except ExtractionError as exc:
            raise ValidationException(
                str(exc),
                code="KB_UNSUPPORTED_FILE_TYPE",
                message="That file type is not supported.",
            ) from exc

    async def _assert_within_limits(self, byte_size: int) -> None:
        limits.assert_within_source_limit(byte_size)
        limits.assert_within_tenant_limit(await self.sources.total_bytes(), byte_size)

    async def _require_unique_name(self, name: str, exclude_id: uuid.UUID | None = None) -> None:
        if await self.knowledge_bases.name_taken(name, exclude_id=exclude_id):
            raise ConflictException(
                "A knowledge base with that name already exists.", code="KB_NAME_TAKEN"
            )
