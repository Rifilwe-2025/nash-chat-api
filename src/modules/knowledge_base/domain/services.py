"""Knowledge base business logic: CRUD, attachment, and ingestion (spec §5.2).

Two things are worth knowing before reading further.

**Extraction failure is not an error response.** A password-protected PDF, a 404 URL, or a CSV with
no rows produces a source in ``FAILED`` status carrying a readable ``error_detail`` — the upload
itself succeeds. That is the phase's bar: a bad document must be inspectable in the API, not a 500.
Limit and type violations are the exception, since they are rejected before anything is stored.

**Cross-module access goes service → service.** Attaching a knowledge base to an agent asks
``AgentService`` for the agent; this module never touches the agents module's repositories or
models. That also means the agent lookup is scoped by the agents module's own rules, so an agent id
from another tenant is simply not found.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

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
from src.modules.knowledge_base.internal import limits
from src.modules.knowledge_base.internal.extractors import (
    ExtractedContent,
    ExtractionError,
    ExtractionResult,
    Extractor,
    get_extractor,
    media_type_for,
)
from src.modules.knowledge_base.internal.fetching import fetch
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
        retrieval_tier: RetrievalTier = RetrievalTier.DIRECT,
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
        """Ingest a web page. The fetch itself may fail — that lands as a failed source."""
        knowledge_base = await self.get(kb_id)

        source = await self.sources.add(
            KbSource(
                kb_id=knowledge_base.id,
                name=name or url,
                type=SourceType.URL,
                status=SourceStatus.PROCESSING,
                config_json={"url": url},
                byte_size=0,
            )
        )

        # Only extraction failures are caught. A storage-limit breach is the tenant's to fix
        # immediately, so it propagates as a 4xx and the row written above goes with the rollback
        # the session dependency performs on the way out.
        try:
            page = await fetch(url, max_bytes=limits.max_source_bytes())
            await self._assert_within_limits(len(page.body))
            # Raised as an extraction failure rather than a 422: the tenant supplied a URL, and
            # what the server on the other end chose to serve is a property of the page.
            media_type = media_type_for(page.url, page.media_type)
            result = await self._extract(
                ExtractedContent(data=page.body, media_type=media_type, filename=page.url)
            )
        except ExtractionError as exc:
            return await self._mark_failed(source, str(exc))

        return await self._mark_ready(
            source,
            result,
            byte_size=len(page.body),
            config={"url": url, "resolvedUrl": page.url, "mediaType": media_type},
        )

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
        content: ExtractedContent,
    ) -> KbSource:
        """Record the source, then extract into it.

        The row is written *before* extraction so a failure has somewhere to be recorded, which is
        also the shape Phase 9 needs when extraction moves onto the queue: the row exists in
        ``PROCESSING`` and a worker finishes it.
        """
        source = await self.sources.add(
            KbSource(
                kb_id=knowledge_base.id,
                name=name,
                type=source_type,
                status=SourceStatus.PROCESSING,
                config_json=config,
                byte_size=byte_size,
            )
        )

        try:
            result = await self._extract(content)
        except ExtractionError as exc:
            return await self._mark_failed(source, str(exc))

        return await self._mark_ready(source, result, byte_size=byte_size)

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
