from __future__ import annotations

import uuid

from pydantic import EmailStr, Field

from src.shared.responses import CamelModel


class UserResponse(CamelModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    role: str
    tenant_id: uuid.UUID


class TenantResponse(CamelModel):
    id: uuid.UUID
    name: str
    plan: str = Field(description="Subscription tier.", examples=["free", "starter", "pro"])


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
