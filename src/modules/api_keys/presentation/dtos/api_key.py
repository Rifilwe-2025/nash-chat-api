"""API key shapes (spec §5.6).

The issue response is the one place in the API that returns a secret, and it is a separate model
from the read shape so that is structurally true rather than a convention: :class:`ApiKeyResponse`
has no field the secret could occupy.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from src.modules.api_keys.domain.models import ApiKeyScope
from src.shared.responses import CamelModel


class IssueApiKeyRequest(CamelModel):
    name: str = Field(
        min_length=1,
        max_length=255,
        description="What this key is for, so you can tell your keys apart later.",
        examples=["Website widget (production)"],
    )
    scopes: list[ApiKeyScope] | None = Field(
        default=None,
        description=(
            "What the key may do. Defaults to both. Give a key only what it needs — a widget that "
            "sends messages does not need to read every past conversation."
        ),
        examples=[["chat:write", "chat:read"]],
    )
    rate_limit_per_minute: int | None = Field(
        default=None,
        ge=1,
        description="Requests per minute for this key. Defaults to your plan's rate.",
        examples=[60],
    )
    expires_at: datetime | None = Field(
        default=None,
        description="Optional expiry. The key stops working at this moment without being revoked.",
    )


class UpdateApiKeyRequest(CamelModel):
    """Every field is optional — omitted fields are left unchanged. The secret never changes."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    scopes: list[ApiKeyScope] | None = None
    rate_limit_per_minute: int | None = Field(default=None, ge=1)


class ApiKeyResponse(CamelModel):
    """A key as it can be read back. The secret is not here and cannot be recovered."""

    id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    prefix: str = Field(
        description="The opening characters of the key, so you can identify it in a list.",
        examples=["nsk_live_7Fq"],
    )
    scopes: list[str]
    rate_limit_per_minute: int
    last_used_at: datetime | None = None
    revoked_at: datetime | None = Field(
        default=None, description="Present once revoked. A revoked key is refused immediately."
    )
    expires_at: datetime | None = None
    active: bool = Field(description="False once the key is revoked or has expired.")
    created_at: datetime


class IssuedApiKeyResponse(CamelModel):
    """**The only response that contains the secret.**

    It is not stored — only a hash of it is — so this is the one and only time it can be read.
    Copy it now; if it is lost, issue a new key and revoke this one.
    """

    key: str = Field(
        description="The secret. Shown once, never again, and not recoverable.",
        examples=["nsk_live_7Fq2xR4mN8pQzT1wV6yU3sA9dK0gH5jL"],
    )
    api_key: ApiKeyResponse
