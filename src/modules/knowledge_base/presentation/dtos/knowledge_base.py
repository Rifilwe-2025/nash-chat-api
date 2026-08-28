"""Knowledge base request and response shapes (spec §5.2).

The list response omits ``extractedText`` and the detail response includes it. That split is
deliberate: extracted text is a whole document, and returning it for every row would make a page of
sources megabytes wide — but being able to read exactly what was extracted from a file is the whole
point of the phase, so a single source returns it in full.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from src.modules.knowledge_base.domain.models import RetrievalTier, SourceStatus, SourceType
from src.shared.responses import CamelModel

MAX_MANUAL_BODY = 100_000


class CreateKnowledgeBaseRequest(CamelModel):
    name: str = Field(
        min_length=1,
        max_length=255,
        description="Display name, unique within your tenant.",
        examples=["Product catalogue"],
    )
    description: str = Field(
        default="",
        max_length=2000,
        description="What this knowledge base covers, for your own reference.",
        examples=["Interior and exterior paint ranges, coverage rates and prices."],
    )
    retrieval_tier: RetrievalTier = Field(
        default=RetrievalTier.DIRECT,
        description=(
            "How the content reaches the prompt. `direct` injects the whole knowledge base; "
            "`keyword` searches it first. Automatic tier selection arrives with retrieval."
        ),
        examples=["direct"],
    )


class UpdateKnowledgeBaseRequest(CamelModel):
    """Every field is optional — omitted fields are left unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    retrieval_tier: RetrievalTier | None = None


class AddUrlSourceRequest(CamelModel):
    url: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "Public http(s) page to ingest. URLs resolving to private or internal addresses are "
            "rejected."
        ),
        examples=["https://example.com/returns-policy"],
    )
    name: str | None = Field(
        default=None,
        max_length=500,
        description="Label for the source. Defaults to the URL.",
        examples=["Returns policy"],
    )

    @field_validator("url")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class AddManualSourceRequest(CamelModel):
    """A FAQ entry typed straight into the builder (spec §5.2)."""

    title: str = Field(
        min_length=1,
        max_length=500,
        description="The question, or a short label for the entry.",
        examples=["How long does delivery take?"],
    )
    body: str = Field(
        min_length=1,
        max_length=MAX_MANUAL_BODY,
        description="The answer, in plain text or Markdown.",
        examples=["Orders placed before 2pm are delivered the next working day in Harare."],
    )


class KnowledgeBaseResponse(CamelModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str
    retrieval_tier: RetrievalTier
    source_count: int = Field(description="Sources in this knowledge base.", examples=[4])
    agent_count: int = Field(description="Agents it is attached to.", examples=[2])
    created_at: datetime
    updated_at: datetime


class KnowledgeBaseSummaryResponse(CamelModel):
    """Trimmed shape for list endpoints."""

    id: uuid.UUID
    name: str
    description: str
    retrieval_tier: RetrievalTier
    updated_at: datetime


class SourceSummaryResponse(CamelModel):
    """One source without its extracted text — fetch the source itself to read that."""

    id: uuid.UUID
    kb_id: uuid.UUID
    name: str
    type: SourceType
    status: SourceStatus = Field(
        description=(
            "`ready` once the text has been extracted, `failed` when it could not be — "
            "`errorDetail` says why."
        ),
        examples=["ready"],
    )
    byte_size: int = Field(
        description="Size of the submitted content, in bytes. Counts against your storage limit.",
        examples=[20480],
    )
    error_detail: str | None = Field(
        default=None, description="Why extraction failed. Absent on a healthy source."
    )
    last_synced_at: datetime | None = Field(
        default=None, description="When extraction last ran for this source."
    )
    source_updated_at: datetime | None = Field(
        default=None, description="When the underlying content last changed."
    )
    created_at: datetime


class SourceResponse(SourceSummaryResponse):
    """A single source, including everything extracted from it."""

    extracted_text: str | None = Field(
        default=None,
        description=(
            "The plain text stored for this source. This is exactly what retrieval will use — "
            "there is no chunking or embedding in v1."
        ),
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="What the source is and what extraction found: filename, media type, "
        "headings, row counts.",
        examples=[{"filename": "prices.csv", "format": "csv", "rows": 42}],
    )


class AttachedAgentsResponse(CamelModel):
    agent_ids: list[uuid.UUID] = Field(
        description="Agents this knowledge base is attached to.",
    )


class StorageUsageResponse(CamelModel):
    """Where the tenant stands against its ingestion limits (spec §5.2)."""

    used_bytes: int = Field(description="Bytes stored across every knowledge base.")
    limit_bytes: int = Field(description="Total bytes this tenant may store.")
    max_source_bytes: int = Field(description="Largest single source accepted.")
