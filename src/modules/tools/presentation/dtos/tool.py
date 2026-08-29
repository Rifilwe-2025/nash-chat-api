"""Tool, policy and call-log shapes (spec §5.2.1).

The one thing to notice: ``authConfig`` goes **in** and never comes back. A tool response carries
``hasCredential`` instead, which answers "is this configured?" without returning a tenant's API key
over the wire. That is the same rule the WhatsApp connection follows, for the same reason — the
platform holds these credentials so the model never has to, and handing them back would undo the
point of holding them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from src.modules.tools.domain.models import (
    HttpMethod,
    ToolAuthType,
    ToolOutcome,
    ToolStatus,
)
from src.shared.responses import CamelModel


class CreateToolRequest(CamelModel):
    """Define a live API call this agent may make."""

    name: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "What the model calls this tool. Letters, digits, underscores and hyphens; must start "
            "with a letter."
        ),
        examples=["check_order_status"],
    )
    description: str = Field(
        min_length=10,
        max_length=2000,
        description=(
            "**This is prompt text, not documentation.** It is the only thing the model reads when "
            "deciding whether this tool answers the question in front of it, so say what it "
            "returns and when to use it. A vague description is a tool that never gets called."
        ),
        examples=[
            "Look up the current status and estimated delivery date of a customer's order by its "
            "order number. Use when a customer asks where their order is."
        ],
    )
    endpoint_url: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "The URL to call. `{placeholders}` are filled from the arguments and must be declared "
            "in `requestSchema`. The host must be on the agent's allowed tool hosts."
        ),
        examples=["https://api.example.com/orders/{orderId}"],
    )
    http_method: HttpMethod = Field(
        default=HttpMethod.GET,
        description=(
            "`DELETE` is deliberately not offered — a model should not be able to destroy a "
            "record on a customer's say-so."
        ),
    )
    auth_type: ToolAuthType = Field(
        default=ToolAuthType.NONE, description="How we authenticate to your API."
    )
    auth_config: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Your credential, injected server-side on every call and never shown to the model or "
            "the customer. `apiKeyHeader` takes `headerName` and `value`; `bearer` takes `value`; "
            "`basic` takes `username` and `password`."
        ),
        examples=[{"headerName": "X-API-Key", "value": "sk_live_…"}],
    )
    request_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "JSON Schema for the arguments. Handed to the model as the function's parameters, and "
            "checked against what it sends back."
        ),
        examples=[
            {
                "type": "object",
                "properties": {"orderId": {"type": "string"}},
                "required": ["orderId"],
            }
        ],
    )
    response_mapping: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Which parts of the response the model sees. `root` narrows to a dotted path; `fields` "
            "maps source paths to labels **and acts as an allowlist** — declare it and nothing "
            "else is included. Omit to send the whole response, truncated."
        ),
        examples=[{"root": "data", "fields": {"status": "Status", "eta": "Arrives"}}],
    )
    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        le=60,
        description="Overrides the platform default. A customer is waiting on this call.",
    )
    cache_ttl_seconds: int = Field(
        default=0,
        ge=0,
        le=3600,
        description=(
            "Seconds an identical call may be served from cache. Leave at 0 for per-customer data "
            "— a cached order status is a wrong order status."
        ),
    )


class UpdateToolRequest(CamelModel):
    """Every field is optional — omitted fields are left unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, min_length=10, max_length=2000)
    endpoint_url: str | None = Field(default=None, min_length=1, max_length=2000)
    http_method: HttpMethod | None = None
    auth_type: ToolAuthType | None = None
    auth_config: dict[str, Any] | None = None
    request_schema: dict[str, Any] | None = None
    response_mapping: dict[str, Any] | None = None
    status: ToolStatus | None = Field(
        default=None,
        description="Set `disabled` to take the tool out of the prompt without deleting it.",
    )
    timeout_seconds: float | None = Field(default=None, gt=0, le=60)
    cache_ttl_seconds: int | None = Field(default=None, ge=0, le=3600)


class ToolResponse(CamelModel):
    """A configured tool. Credentials are never included."""

    id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    description: str
    endpoint_url: str
    http_method: HttpMethod
    auth_type: ToolAuthType
    has_credential: bool = Field(
        description="Whether a credential is stored. The value itself is never returned."
    )
    request_schema: dict[str, Any]
    response_mapping: dict[str, Any]
    status: ToolStatus
    timeout_seconds: float | None = None
    cache_ttl_seconds: int
    last_called_at: datetime | None = None
    consecutive_failures: int = Field(
        description="Consecutive failed calls. Resets on success — a rising count means your API "
        "is rejecting or timing out."
    )
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class ToolPolicyRequest(CamelModel):
    """The per-agent limits every one of its tools is bound by."""

    allowed_hosts: list[str] | None = Field(
        default=None,
        description=(
            "Hostnames this agent's tools may call. A leading dot allows subdomains "
            "(`.example.com` matches `api.example.com`); without one the match is exact. "
            "**An empty list means no tool can run.**"
        ),
        examples=[["api.example.com", ".partner.example"]],
    )
    max_calls_per_turn: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description="Ceiling on tool calls while answering one message. Bounds cost and loops.",
    )


class ToolPolicyResponse(CamelModel):
    agent_id: uuid.UUID
    allowed_hosts: list[str]
    max_calls_per_turn: int
    updated_at: datetime


class TryToolRequest(CamelModel):
    """Run a tool yourself, with arguments you choose, before trusting it to a customer."""

    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="The arguments a model would supply. Checked against `requestSchema`.",
        examples=[{"orderId": "A-10432"}],
    )


class ToolCallResponse(CamelModel):
    """One execution, as the call log records it."""

    id: uuid.UUID
    outcome: ToolOutcome = Field(
        description=(
            "`succeeded` or `cached` worked. `refused` means our own guards stopped it — a host "
            "off the allowlist, or arguments that did not match the schema — and is a "
            "configuration problem. `failed` and `timedOut` are your API."
        )
    )
    arguments: dict[str, Any] = Field(
        description="What the model asked for. Kept even when the call was refused."
    )
    status_code: int | None = None
    duration_ms: int
    result_text: str | None = Field(
        default=None, description="Exactly what the model was shown, after mapping and truncation."
    )
    error_detail: str | None = None
    conversation_id: uuid.UUID | None = None
    created_at: datetime


class TryToolResponse(CamelModel):
    """The result of a test run, including what the model would have been told."""

    outcome: ToolOutcome
    duration_ms: int
    result_text: str = Field(
        description=(
            "The text a model would receive. On a failure this is the note it would compose an "
            "apology from, not an error — which is what you want to read before going live."
        )
    )
    call_id: uuid.UUID | None = None
