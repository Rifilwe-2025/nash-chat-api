"""The platform event log (spec §5.8, "error/failure tracking").

Analytics is a **read model** over what the other modules already store: messages carry their own
tokens and cost, a broken source carries its own error, a tool call carries its own outcome. This is
the one table it owns, and it exists because of a gap those rows cannot close.

**A failure that rolls back leaves no row.** When a provider call fails mid-turn the whole
transaction goes with it — the user message, the conversation, everything — so a WhatsApp customer
is left with no answer and the database says nothing happened. That failure has no durable home
anywhere else in the schema, so it gets one here, written from the worker's final attempt in a
session of its own so that it survives the rollback that caused it.

**What is deliberately not written here.** Failed ingestions (``kb_source.status``), refused or
timed-out tool calls (``tool_call.outcome``), undelivered WhatsApp messages
(``whatsapp_message.status``) and failing webhook endpoints (``webhook_endpoint.failure_count``)
already have durable rows owned by the modules that understand them. Copying them into a second
table would create two records of one fact, free to disagree — the failure report reads each of them
where it lives and assembles the result at the service layer instead.

**Not every provider error reaches this table, and that is correct.** A synchronous chat call that
fails answers its caller with a 409 and a request id: they were told, immediately, by the response.
It is the failures nobody is waiting on that need somewhere to be found later. Aggregate counts of
*every* provider error, including the ones that answered a caller, live in the process metrics
registry (``shared/observability``), which is what the operator endpoint reads.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base_model import TenantScopedModel, enum_column


class EventCategory(str, enum.Enum):
    """What kind of failure this was.

    One member, which is the point rather than an oversight: this table exists for failures with no
    other durable home, and today there is exactly one. A webhook that keeps failing is already
    counted on ``webhook_endpoint.failure_count``, an undelivered WhatsApp message on
    ``whatsapp_message.status``, a broken source on ``kb_source.status``. Adding categories for
    those would create a second record of each fact, free to disagree with the first.

    The enum exists rather than a bare string so the next genuinely homeless failure joins it as a
    value and a migration, not as an untyped literal in a service somewhere.
    """

    PROVIDER_ERROR = "provider_error"


class PlatformEvent(TenantScopedModel):
    """One recorded failure, with enough context to act on it and nothing more.

    ``detail`` is bounded and is written from our own error text, never from a provider's raw
    response body: an upstream error page can be a megabyte of HTML, and a failure log that stores
    it is a failure log nobody can read.
    """

    __tablename__ = "platform_event"
    __table_args__ = (
        # Every read is "this tenant's failures, newest first, optionally of one kind", so the
        # index leads with the tenant and ends with time.
        Index("ix_platform_event_tenant_created", "tenant_id", "created_at"),
        Index("ix_platform_event_tenant_category", "tenant_id", "category", "created_at"),
    )

    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        doc="Null for a failure that belongs to the tenant rather than to one agent.",
    )
    category: Mapped[EventCategory] = mapped_column(
        enum_column(EventCategory, "platform_event_category"), nullable=False
    )
    # A stable, machine-readable reason a dashboard can group by — "PROVIDER_UNAVAILABLE",
    # "HTTP_500". Distinct from `detail`, which is prose for a person reading one row.
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # The endpoint that failed, the provider and model in play, the message id that went unanswered.
    # Nothing queries inside it; it is what makes one row actionable when someone opens it.
    meta_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
