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
from src.modules.knowledge_base.internal.connectors import AuthType, PaginationStyle
from src.modules.knowledge_base.internal.retrieval import NoContextReason
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
        default=RetrievalTier.AUTO,
        description=(
            "How the content reaches the prompt. Leave on `auto` and the tier is chosen per query "
            "from how much text the knowledge base holds, so it keeps working as it grows. "
            "`direct` always injects the whole knowledge base; `keyword` always searches it first."
        ),
        examples=["auto"],
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


class AddApiSourceRequest(CamelModel):
    """A Pattern B connector: an API pulled on a schedule and indexed (spec §5.2.1)."""

    name: str = Field(
        min_length=1,
        max_length=500,
        description="Label for the source.",
        examples=["Product catalogue"],
    )
    url: str = Field(
        min_length=1,
        max_length=2000,
        description="The JSON endpoint to pull. Must be publicly reachable.",
        examples=["https://shop.example.com/api/products"],
    )
    content_fields: list[str] = Field(
        min_length=1,
        description=(
            "Which fields become the indexed text, in order. Dotted paths reach into nested "
            "records. These are turned into sentences rather than injected as raw JSON."
        ),
        examples=[["sku", "name", "description", "price"]],
    )
    metadata_fields: list[str] = Field(
        default_factory=list,
        description="Fields kept for citation and filtering but left out of the prompt.",
        examples=[["category", "updated_at"]],
    )
    id_field: str = Field(
        default="id", max_length=200, description="Field identifying each record."
    )
    version_field: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Field carrying the record's version or last-modified time. Supply it and an "
            "unchanged record is skipped on re-sync; without it a content hash is used instead."
        ),
        examples=["updated_at"],
    )
    records_path: str | None = Field(
        default=None,
        max_length=200,
        description="Where the record list sits in the response. Omit if the body is the list.",
        examples=["data.items"],
    )
    pagination: PaginationStyle = Field(
        default=PaginationStyle.NONE, description="How to walk past the first page."
    )
    page_size: int | None = Field(default=None, ge=1, le=1000)
    auth_type: AuthType = Field(default=AuthType.NONE, description="How to authenticate.")
    credentials: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Auth values for the chosen type — `token` for bearer, `header`/`value` for an API "
            "key, `username`/`password` for basic. Stored server-side and never returned."
        ),
    )
    sync_interval_minutes: int | None = Field(
        default=None,
        ge=0,
        description=(
            "How often to re-pull, in minutes. 0 never re-syncs. Below the configured floor is "
            "rejected — a too-frequent sync gets you rate limited by your own supplier."
        ),
        examples=[60],
    )


class SyncScheduleRequest(CamelModel):
    sync_interval_minutes: int = Field(
        ge=0,
        description="Minutes between automatic re-syncs. 0 stops scheduling.",
        examples=[1440],
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
    sync_interval_minutes: int = Field(
        description="Minutes between automatic re-syncs. 0 means it is never re-synced.",
        examples=[60],
    )
    next_sync_at: datetime | None = Field(
        default=None, description="When the next automatic sync is due."
    )
    consecutive_failures: int = Field(
        description=(
            "Failed syncs in a row. A rising count usually means expired credentials or a changed "
            "response shape."
        )
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


class RetrievalRequest(CamelModel):
    """Ask what a query would pull out of a knowledge base, and how (spec §5.2)."""

    query: str = Field(
        min_length=1,
        max_length=2000,
        description="The question to retrieve for, as an end user would phrase it.",
        examples=["Can I return tinted paint?"],
    )
    model: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Model the answer is destined for. Its context window sets the injection budget, so "
            "the tier chosen can differ between models. Defaults to a conservative budget."
        ),
        examples=["gemini-2.0-flash"],
    )


class CitationResponse(CamelModel):
    """Where a passage came from — carried on every result, for logging and debugging."""

    source_id: uuid.UUID
    kb_id: uuid.UUID
    source_name: str = Field(examples=["Returns policy.docx"])
    source_type: SourceType
    url: str | None = Field(default=None, description="Present for sources ingested from a URL.")


class PassageResponse(CamelModel):
    text: str = Field(
        description=(
            "The knowledge itself. Under `direct` this is a whole source; under `keyword` it is "
            "the fragment matched around the query, cut out at search time — not a stored chunk."
        )
    )
    citation: CitationResponse
    score: float | None = Field(
        default=None,
        description="Relevance rank. Present for `keyword` results only; `direct` does not rank.",
        examples=[0.138],
    )


class RetrievalExplainResponse(CamelModel):
    """What a query would pull, and why that tier ran."""

    tier: RetrievalTier = Field(description="The tier that ran.", examples=["keyword"])
    tier_forced: bool = Field(
        description="True when the knowledge base pins the tier rather than choosing per query."
    )
    tier_reason: str = Field(
        description="Why this tier ran, in plain language.",
        examples=[
            "184320 characters of knowledge exceed the 48000 character budget, so the query is "
            "searched instead"
        ],
    )
    considered_characters: int = Field(
        description="Characters of extracted text across the knowledge bases in scope."
    )
    budget_characters: int = Field(
        description="Characters that may be injected whole before searching is used instead."
    )
    has_context: bool = Field(
        description=(
            "False when nothing relevant was found. This is an answer, not a failure: an agent "
            "seeing it uses its configured fallback response rather than guessing."
        )
    )
    no_context_reason: NoContextReason | None = Field(
        default=None,
        description=(
            "Why nothing came back — `empty_knowledge_base`, `no_match`, or `below_threshold`."
        ),
    )
    passages: list[PassageResponse] = Field(
        default_factory=list, description="What would be injected, in the order it would appear."
    )
    retrieved_characters: int = Field(description="Total size of the passages above.")
