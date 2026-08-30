from __future__ import annotations

import uuid

from pydantic import EmailStr, Field

from src.shared.responses import CamelModel


class UserResponse(CamelModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    role: str = Field(description="Your role inside your own organisation.")
    tenant_id: uuid.UUID
    is_platform_admin: bool = Field(
        default=False,
        description=(
            "Platform staff. Grants the `/admin` routes and the ability to act on any account. "
            "Granted out of band by whoever runs the deployment, never through this API."
        ),
    )


class TenantResponse(CamelModel):
    id: uuid.UUID
    name: str
    plan: str = Field(
        description="A label on the account. No plan limits are enforced.",
        examples=["free", "starter", "pro"],
    )
    status: str = Field(
        description=(
            "`active` or `disabled`. A disabled account cannot sign in and its agents serve no "
            "traffic, so you will not normally see this value — the request that would "
            "return it is refused first."
        ),
        examples=["active"],
    )


class MeResponse(CamelModel):
    user: UserResponse
    tenant: TenantResponse


class UpdateProfileRequest(CamelModel):
    full_name: str | None = Field(
        default=None, max_length=255, description="New display name. Omit to leave unchanged."
    )
    email: EmailStr | None = Field(
        default=None, description="New email address. Must not belong to another account."
    )


class UpdateTenantRequest(CamelModel):
    name: str = Field(min_length=1, max_length=255, description="New organisation name.")
