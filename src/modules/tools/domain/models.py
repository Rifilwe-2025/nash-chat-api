"""Agent tools: live API calls the model can choose to make (spec §5.2.1 Pattern A, §7).

Pattern A is the opposite of Pattern B in every way that matters. Pattern B (Phase 9) pulls an API
on a schedule and *indexes* what it finds, so the answer is as fresh as the last sync. A tool is
called **at query time, for this customer, about their own data** — an order status, a booking, a
balance — which is exactly the data that cannot be indexed: it is different per person and stale the
moment it is stored.

Three tables, and each exists for a reason the others cannot cover.

``agent_tool`` is the definition a tenant writes. Its ``description`` is load-bearing in a way no
other text column in this schema is: it is not documentation, it is **the prompt** — the only thing
the model reads when deciding whether this tool answers the question in front of it. A vague
description is a tool that never gets called, or one that gets called for everything.

``tool_policy`` is the per-agent endpoint allowlist §5.2.1 asks for, plus the ceiling on how many
calls one turn may make. It is a separate row rather than columns on ``agent_tool`` because it
governs *the agent*, not any one tool: a policy that lived on each tool could be widened by adding
another tool, which is precisely the thing an allowlist exists to prevent.

``tool_call`` is the log — arguments, latency, outcome. Tools fail in ways knowledge never does
(someone else's API changed, a token expired, a network was slow), and the only way to tell "the
agent gave a bad answer" from "the tool returned bad data" is to have kept what the tool returned.

**Credentials live in ``auth_config_json`` and are never sent to the model or the client.** The
whole security argument of Pattern A is that the platform holds the tenant's API key and injects it
server-side (§5.2.1); a tool whose credential reached the prompt would be a credential the model
could be talked into repeating. The column encrypts itself at rest (Phase 13, §5.7).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.crypto import EncryptedJson
from src.shared.database.base_model import BaseModel, TenantScopedModel, enum_column


class HttpMethod(str, enum.Enum):
    """What the tool does to its endpoint.

    Deliberately no ``DELETE``. A tool is chosen by a language model on a customer's say-so, and
    the blast radius of a wrong choice should not include destroying a record. ``GET`` and ``POST``
    cover lookups and the submit-a-form cases v1 is for; anything destructive is a decision a person
    should still be making.
    """

    GET = "get"
    POST = "post"
    PUT = "put"
    PATCH = "patch"


class ToolAuthType(str, enum.Enum):
    """How the platform authenticates to the tenant's API on their behalf.

    ``OAUTH`` is absent: it needs a token refresh cycle and a place to store a refresh token, which
    is a feature rather than an enum member. The three here cover what a small business's API
    actually offers, and adding OAuth later does not change any of them.
    """

    NONE = "none"
    API_KEY_HEADER = "api_key_header"
    BEARER = "bearer"
    BASIC = "basic"


class ToolStatus(str, enum.Enum):
    """``DISABLED`` keeps the definition but takes it out of the prompt.

    A tenant debugging a misbehaving integration needs to stop it reaching customers *now*, without
    losing the configuration they spent an afternoon getting right.
    """

    ENABLED = "enabled"
    DISABLED = "disabled"


class ToolOutcome(str, enum.Enum):
    """How one execution ended.

    The distinctions are the ones worth acting on. ``REFUSED`` means our own guards stopped it — a
    host off the allowlist, arguments that did not match the schema — and is a configuration bug.
    ``FAILED`` and ``TIMED_OUT`` are the tenant's API misbehaving. ``CACHED`` is a call that never
    left the building. Collapsing these into "error" would mean a dashboard that cannot tell whose
    fault anything is.
    """

    SUCCEEDED = "succeeded"
    CACHED = "cached"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    REFUSED = "refused"


class AgentTool(TenantScopedModel):
    """One live API call an agent may make."""

    __tablename__ = "agent_tool"
    __table_args__ = (
        # The model addresses a tool by name, so a duplicate within one agent would be ambiguous
        # exactly where ambiguity is most expensive.
        UniqueConstraint("agent_id", "name", name="uq_agent_tool_agent_id_name"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The identifier the model calls. Constrained to what every provider accepts for a function
    # name — validated in the service, since a database check constraint could not explain itself.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="What the model reads to decide whether this tool answers the question. This is prompt "
        "text, not documentation.",
    )
    endpoint_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    http_method: Mapped[HttpMethod] = mapped_column(
        enum_column(HttpMethod, "tool_http_method"),
        nullable=False,
        default=HttpMethod.GET,
        server_default=HttpMethod.GET.value,
    )
    auth_type: Mapped[ToolAuthType] = mapped_column(
        enum_column(ToolAuthType, "tool_auth_type"),
        nullable=False,
        default=ToolAuthType.NONE,
        server_default=ToolAuthType.NONE.value,
    )
    # The tenant's credential for their own API. Stored, not hashed — it has to be presented — and
    # encrypted at rest by the column type (§5.7), so no service can forget to apply it.
    auth_config_json: Mapped[dict[str, Any]] = mapped_column(
        EncryptedJson, nullable=False, default=dict, server_default="{}"
    )
    # JSON Schema for the arguments, handed to the provider verbatim as the function's parameters.
    request_schema_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # How the response becomes text the model can read: which fields to keep, what to call them.
    response_mapping_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    status: Mapped[ToolStatus] = mapped_column(
        enum_column(ToolStatus, "tool_status"),
        nullable=False,
        default=ToolStatus.ENABLED,
        server_default=ToolStatus.ENABLED.value,
    )
    timeout_seconds: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        doc="Overrides the platform default. A customer is waiting on this call.",
    )
    cache_ttl_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        doc="Seconds an identical call may be served from cache. Zero disables caching, which is "
        "the right default for per-customer data.",
    )
    last_called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class ToolPolicy(TenantScopedModel):
    """The per-agent limits every one of that agent's tools is bound by (spec §5.2.1).

    One row per agent, created the first time a tool is added. Separate from ``agent_tool`` because
    an allowlist that lived on the thing it constrains could be widened by whoever adds the next
    tool — the point of the list is that it is decided once, above them all.
    """

    __tablename__ = "tool_policy"
    __table_args__ = (UniqueConstraint("agent_id", name="uq_tool_policy_agent_id"),)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Hostnames this agent's tools may reach. Empty means nothing is allowed *and no tool runs* —
    # fail closed, so a tenant who has not thought about it does not get an agent that can call
    # anywhere. The service seeds it with a tool's own host when the first tool is created.
    allowed_hosts: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    max_calls_per_turn: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=3,
        server_default="3",
        doc="Ceiling on tool calls in one turn. Bounds both cost and the loop where a model keeps "
        "asking for the same tool.",
    )


class ToolCallLog(BaseModel):
    """What happened when a tool ran (spec §5.2.1, and the error tracking §5.8 asks for).

    Not tenant-scoped: a call is only ever reached through the tool or conversation it belongs to,
    both of which are. The same reasoning the ``message`` table uses.

    ``arguments_json`` is what the *model* asked for, and is worth keeping even when the call was
    refused — a tool called with nonsense arguments is a description problem, and this is the only
    place that shows it.
    """

    __tablename__ = "tool_call"
    __table_args__ = (Index("ix_tool_call_tool_id_created_at", "tool_id", "created_at"),)

    tool_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_tool.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversation.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Null for a tenant's own test run, which belongs to no conversation.",
    )
    outcome: Mapped[ToolOutcome] = mapped_column(enum_column(ToolOutcome, "tool_outcome"))
    arguments_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # What the model was told. Truncated to the same budget the prompt uses, because storing more
    # than was sent would make this log a record of something that never happened.
    result_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
