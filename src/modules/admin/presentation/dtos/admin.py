"""Platform administration shapes.

These describe *accounts*, never what is inside one. There is no agent configuration, no transcript
and no knowledge here — an admin reads those by acting as the tenant, through the same endpoints the
tenant uses. Keeping that line visible in the DTOs is part of keeping it true in the code.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from src.modules.tenants.domain.models import TenantPlan, TenantStatus, UserRole
from src.shared.responses import CamelModel


class TenantCountsResponse(CamelModel):
    """How big an account is, without looking inside it."""

    users: int
    agents: int
    conversations: int
    messages: int
    stored_bytes: int = Field(
        description="Bytes of submitted knowledge held for this account.", examples=[10485760]
    )


class TenantSummaryResponse(CamelModel):
    id: uuid.UUID
    name: str
    plan: TenantPlan = Field(
        description="A label on the account. The platform enforces no plan limits."
    )
    status: TenantStatus = Field(
        description="`disabled` means nobody can sign in and no agent answers on any channel."
    )
    status_note: str | None = Field(
        default=None, description="Why it was set that way. Never shown to the account holder."
    )
    status_changed_at: datetime | None = None
    counts: TenantCountsResponse
    created_at: datetime


class AdminUserResponse(CamelModel):
    id: uuid.UUID
    email: str
    full_name: str | None = None
    role: UserRole = Field(description="Their role inside their own tenant.")
    is_platform_admin: bool = Field(
        description="Platform staff. Granted out of band, never through this API."
    )
    created_at: datetime


class TenantDetailResponse(TenantSummaryResponse):
    """One account, with the people in it."""

    users: list[AdminUserResponse]


class SetTenantStatusRequest(CamelModel):
    enabled: bool = Field(
        description=(
            "`false` disables the account: nobody can sign in, its API keys are refused, and its "
            "agents stop answering on every channel. Nothing is deleted and it can be re-enabled."
        )
    )
    note: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Why, for whoever finds the account disabled later. Kept on the tenant and never "
            "returned to the account holder."
        ),
        examples=["Suspended pending review of reported content."],
    )


class PlatformOverviewResponse(CamelModel):
    """The whole deployment in one row."""

    tenants: int
    active_tenants: int
    disabled_tenants: int
    users: int
    agents: int
    conversations: int
