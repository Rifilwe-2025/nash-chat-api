"""PDFs and images through the whole ingestion path (spec §5.2.3).

Exercised at the service rather than over HTTP, because this is the one format whose extraction
calls a provider: the extractor is injected here so the pipeline — record the source, read the
file, store the text and the token cost — is covered without real credentials. The HTTP surface
around it is identical to the other formats and is covered in ``test_sources.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.knowledge_base.domain.models import SourceStatus
from src.modules.knowledge_base.domain.services import KnowledgeBaseService
from src.modules.knowledge_base.internal.extractors import (
    ExtractedContent,
    ExtractionError,
    ExtractionResult,
)
from src.modules.tenants.domain.models import Tenant

PDF = b"%PDF-1.7 a scanned price list"


class FakeLlmExtractor:
    """Stands in for the model reading the file."""

    def __init__(self, text: str = "Matt white 5L costs $45.99.") -> None:
        self.seen: list[ExtractedContent] = []
        self._text = text

    async def extract(self, content: ExtractedContent) -> ExtractionResult:
        self.seen.append(content)
        return ExtractionResult(
            text=self._text,
            metadata={"format": "llm", "extractionTokens": 150, "mediaType": content.media_type},
        )


class UnreadableFile(FakeLlmExtractor):
    async def extract(self, content: ExtractedContent) -> ExtractionResult:
        raise ExtractionError("The file is password protected and could not be opened.")


@pytest.fixture
async def tenant(make_tenant: Callable[..., Coroutine[Any, Any, Tenant]]) -> Tenant:
    return await make_tenant(name="Nash Paints")


async def test_a_pdf_is_stored_as_the_text_the_model_read(
    session: AsyncSession, tenant: Tenant
) -> None:
    extractor = FakeLlmExtractor()
    service = KnowledgeBaseService(session, tenant.id, llm_extractor=extractor)
    knowledge_base = await service.create(name="Catalogue")

    source = await service.add_file_source(
        knowledge_base.id, filename="prices.pdf", data=PDF, declared_media_type="application/pdf"
    )

    assert source.status is SourceStatus.READY
    assert source.extracted_text == "Matt white 5L costs $45.99."
    assert source.byte_size == len(PDF)
    assert source.last_synced_at is not None
    assert extractor.seen[0].media_type == "application/pdf"
    assert extractor.seen[0].data == PDF


async def test_the_extraction_cost_is_recorded_on_the_source(
    session: AsyncSession, tenant: Tenant
) -> None:
    """Token counts are the phase-4 contract; ingestion is a place they are actually spent."""
    service = KnowledgeBaseService(session, tenant.id, llm_extractor=FakeLlmExtractor())
    knowledge_base = await service.create(name="Catalogue")

    source = await service.add_file_source(
        knowledge_base.id, filename="prices.pdf", data=PDF, declared_media_type="application/pdf"
    )

    assert source.config_json["extractionTokens"] == 150
    assert source.config_json["filename"] == "prices.pdf"


async def test_an_image_takes_the_same_path_as_a_pdf(session: AsyncSession, tenant: Tenant) -> None:
    extractor = FakeLlmExtractor(text="A shelf of 5L paint tins, priced $45.99 each.")
    service = KnowledgeBaseService(session, tenant.id, llm_extractor=extractor)
    knowledge_base = await service.create(name="Catalogue")

    source = await service.add_file_source(
        knowledge_base.id,
        filename="shelf.png",
        data=b"\x89PNG a photo of a shelf",
        declared_media_type="image/png",
    )

    assert source.status is SourceStatus.READY
    assert source.extracted_text == "A shelf of 5L paint tins, priced $45.99 each."
    assert extractor.seen[0].media_type == "image/png"


async def test_a_file_the_model_cannot_open_becomes_a_readable_failure(
    session: AsyncSession, tenant: Tenant
) -> None:
    service = KnowledgeBaseService(session, tenant.id, llm_extractor=UnreadableFile())
    knowledge_base = await service.create(name="Catalogue")

    source = await service.add_file_source(
        knowledge_base.id, filename="locked.pdf", data=PDF, declared_media_type="application/pdf"
    )

    assert source.status is SourceStatus.FAILED
    assert source.error_detail == "The file is password protected and could not be opened."
    assert source.extracted_text is None
    assert source.byte_size == 0, "nothing usable was stored, so nothing is charged for"


async def test_the_stored_text_is_what_retrieval_will_read_back(
    session: AsyncSession, tenant: Tenant
) -> None:
    """No chunking, no embeddings: what Phase 6 assembles is exactly this column (spec §5.2.2)."""
    service = KnowledgeBaseService(session, tenant.id, llm_extractor=FakeLlmExtractor())
    knowledge_base = await service.create(name="Catalogue")
    await service.add_file_source(
        knowledge_base.id, filename="prices.pdf", data=PDF, declared_media_type="application/pdf"
    )
    await service.add_manual_source(knowledge_base.id, title="Delivery", body="Next working day.")

    sources = await service.sources.all_for_kb(knowledge_base.id)

    assert [source.extracted_text for source in sources] == [
        "Matt white 5L costs $45.99.",
        "# Delivery\n\nNext working day.",
    ]
