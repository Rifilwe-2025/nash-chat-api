"""Knowledge bases, their sources, and the agents they serve (spec §5.2, §7).

Three tables, and the shape of each follows from one decision: **v1 stores knowledge as plain
extracted text, not vectors** (§5.2.2). There is no `kb_chunk` table, no embedding column, and no
chunk pipeline — a source holds the whole extracted document in `extracted_text`, and retrieval
(Phase 6) either injects it whole or searches it with Postgres full-text search. That is what makes
the v2 vector upgrade additive: chunking reads from this same column instead of replacing it.

`kb_source` carries `tenant_id` even though it already reaches a tenant through its knowledge base.
The denormalisation is deliberate: it lets sources be read and totalled through a
`TenantScopedRepository`, so the storage-limit query and every source read are scoped by the query
layer rather than by remembering to join (spec §5.7).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.database.base_model import BaseModel, TenantScopedModel, enum_column


class RetrievalTier(str, enum.Enum):
    """How this KB's content reaches the prompt (spec §5.2.2).

    ``AUTO`` is the default and the one most tenants should stay on: the tier is chosen per query
    from how much text the knowledge base actually holds, so a KB that grows past what can be
    injected starts being searched instead, with no configuration change. ``DIRECT`` and
    ``KEYWORD`` pin the choice for a tenant who knows better than the heuristic.

    ``vector`` is a v2 tier and deliberately absent: adding the value before the pipeline exists
    would let a tenant select a tier that silently does nothing.
    """

    AUTO = "auto"
    DIRECT = "direct"
    KEYWORD = "keyword"


class SourceType(str, enum.Enum):
    """Where a source's content came from.

    ``API_INDEXED`` is Pattern B (spec §5.2.1): a connector pulls records from an API on a schedule
    and feeds them through the same extraction path as a file. It is *indexed* content — Pattern A,
    the live tool call at query time, is a different thing entirely and arrives in Phase 11.
    """

    FILE = "file"
    URL = "url"
    MANUAL = "manual"
    API_INDEXED = "api_indexed"


class SourceStatus(str, enum.Enum):
    """Extraction lifecycle (spec §5.2: "source status tracking").

    ``PENDING`` exists for Phase 9, when extraction moves off the request path onto the queue. In
    this phase extraction runs inline, so a source is ``READY`` or ``FAILED`` by the time the upload
    responds.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class KnowledgeBase(TenantScopedModel):
    """A reusable body of knowledge. One KB can serve several agents (spec §5.2)."""

    __tablename__ = "knowledge_base"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_knowledge_base_tenant_id_name"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    retrieval_tier: Mapped[RetrievalTier] = mapped_column(
        enum_column(RetrievalTier, "retrieval_tier"),
        nullable=False,
        default=RetrievalTier.AUTO,
        server_default=RetrievalTier.AUTO.value,
    )

    sources: Mapped[list[KbSource]] = relationship(
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
        order_by="KbSource.created_at",
    )
    agent_links: Mapped[list[AgentKbLink]] = relationship(
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
    )


class KbSource(TenantScopedModel):
    """One document, page, or FAQ entry, plus the text extracted from it.

    ``config_json`` holds what the source *is* rather than what it says — the URL fetched, the
    original filename and media type, the CSV columns seen. Retrieval and the source list read it;
    nothing queries inside it, so it stays JSON rather than becoming columns.
    """

    __tablename__ = "kb_source"
    __table_args__ = (Index("ix_kb_source_search_vector", "search_vector", postgresql_using="gin"),)

    kb_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_base.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    type: Mapped[SourceType] = mapped_column(
        enum_column(SourceType, "kb_source_type"),
        nullable=False,
    )
    status: Mapped[SourceStatus] = mapped_column(
        enum_column(SourceStatus, "kb_source_status"),
        nullable=False,
        default=SourceStatus.PENDING,
        server_default=SourceStatus.PENDING.value,
        index=True,
    )
    config_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Size of the *submitted* content, not of the extracted text: it is what the upload limit and
    # the per-tenant storage total are measured against.
    byte_size: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # -- scheduling (spec §5.2.1 Pattern B) ---------------------------------------------
    #
    # The interval lives on the row rather than in a static scheduler config, because a tenant
    # changes it and a scheduler restart is not an acceptable cost for that. A sweep asks the
    # database what is due; see `internal/tasks.py`.
    sync_interval_minutes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        doc="Zero means the source is never re-synced automatically.",
    )
    next_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # Consecutive failures. A source that keeps failing is reported to the tenant rather than
    # quietly rotting — an expired credential or a changed schema is the usual cause (§5.2.1).
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # The source's own version marker from the last successful pull — an ETag, a `last_modified`,
    # or a content hash. It is what lets an unchanged record be skipped instead of re-extracted.
    sync_cursor: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Tier 2's index (spec §5.2.2). A generated column rather than a trigger or an application
    # write: Postgres recomputes it whenever the text changes, so the index cannot drift out of
    # step with ``extracted_text``. The source name is weighted above the body so a query naming a
    # document ranks that document first.
    #
    # This is **not** chunking. There is one vector per source, over the text already stored; the
    # relevant passage is cut out at query time by ``ts_headline``. No ``kb_chunk`` table, no
    # embeddings — that remains v2 (§5.2.4).
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(name, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(extracted_text, '')), 'B')",
            persisted=True,
        ),
        nullable=True,
    )

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="sources")


class AgentKbLink(BaseModel):
    """Many-to-many between agents and knowledge bases (spec §7).

    Not tenant-scoped, and it does not need to be: both ends are loaded through their own scoped
    services before a link is written, so a row can only ever join two objects the caller already
    owns. The unique constraint makes attaching twice a no-op rather than a duplicate.
    """

    __tablename__ = "agent_kb_link"
    __table_args__ = (
        UniqueConstraint("agent_id", "kb_id", name="uq_agent_kb_link_agent_id_kb_id"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_base.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="agent_links")
