"""Personal access token shapes.

As with API keys, the issue response is a separate model from the read shape, so it is structurally
true — not a convention — that :class:`PersonalTokenResponse` has no field a secret could occupy.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from src.modules.mcp.domain.models import McpScope
from src.shared.responses import CamelModel


class IssuePersonalTokenRequest(CamelModel):
    name: str = Field(
        min_length=1,
        max_length=255,
        description="What this token is for — which machine and which coding agent.",
        examples=["Claude Code on my laptop"],
    )
    scopes: list[McpScope] | None = Field(
        default=None,
        min_length=1,
        description=(
            "What the token may do. Defaults to `mcp:read`. `mcp:write` lets a coding agent create "
            "and change agents, knowledge and tools, and includes `mcp:read`."
        ),
        examples=[["mcp:read"], ["mcp:read", "mcp:write"]],
    )
    expires_at: datetime | None = Field(
        default=None,
        description=(
            "Optional expiry. The token stops working at this moment without being revoked. A "
            "token that sits in an editor's configuration file is worth giving an end date."
        ),
    )


class PersonalTokenResponse(CamelModel):
    """A token as it can be read back. The secret is not here and cannot be recovered."""

    id: uuid.UUID
    name: str
    prefix: str = Field(
        description="The opening characters of the token, so you can identify it in a list.",
        examples=["nsp_live_Q7x"],
    )
    scopes: list[str]
    last_used_at: datetime | None = Field(
        default=None,
        description="When a coding agent last used it, to the nearest minute.",
    )
    revoked_at: datetime | None = Field(
        default=None, description="Present once revoked. A revoked token is refused immediately."
    )
    expires_at: datetime | None = None
    active: bool = Field(description="False once the token is revoked or has expired.")
    created_at: datetime


class IssuedPersonalTokenResponse(CamelModel):
    """**The only response that contains the secret.** Copy it now — it is not stored."""

    token: str = Field(
        description="The secret. Shown once, never again, and not recoverable.",
        examples=["nsp_live_Q7xR4mN8pQzT1wV6yU3sA9dK0gH5jLc2bE8fW1nY4tM"],
    )
    personal_token: PersonalTokenResponse


class McpScopeResponse(CamelModel):
    scope: McpScope
    description: str


class McpConnectionResponse(CamelModel):
    """Everything a coding agent's configuration needs, apart from the token itself."""

    enabled: bool = Field(
        description="Whether this deployment serves the MCP endpoint at all (`MCP_ENABLED`)."
    )
    server_url: str = Field(
        description=(
            "The MCP endpoint, built from `PUBLIC_BASE_URL` so it is the address a developer's "
            "machine can reach rather than one internal to the deployment."
        ),
        examples=["https://api.example.com/mcp"],
    )
    transport: str = Field(
        description="The MCP transport the endpoint speaks.", examples=["streamable-http"]
    )
    auth_header: str = Field(
        description="The header the token travels in, with a placeholder for the token.",
        examples=["Authorization: Bearer <token>"],
    )
    scopes: list[McpScopeResponse]
    rate_limit_per_minute: int = Field(description="Requests per minute allowed per token.")
