from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import EmailStr, Field

from src.shared.responses import CamelModel

PASSWORD_MIN_LENGTH = 12


class SignupRequest(CamelModel):
    email: EmailStr = Field(
        description="Email for the first user. Must not already be registered.",
        examples=["owner@acme.com"],
    )
    password: str = Field(
        min_length=PASSWORD_MIN_LENGTH,
        max_length=128,
        description=f"At least {PASSWORD_MIN_LENGTH} characters.",
        examples=["correct-horse-battery"],
    )
    tenant_name: str = Field(
        min_length=1,
        max_length=255,
        description="Name of the organisation this account owns.",
        examples=["Acme Paints"],
    )
    full_name: str | None = Field(
        default=None, max_length=255, description="Optional display name for the user."
    )


class LoginRequest(CamelModel):
    email: EmailStr = Field(description="Registered email address.", examples=["owner@acme.com"])
    password: str = Field(description="Account password.", examples=["correct-horse-battery"])


class RefreshRequest(CamelModel):
    refresh_token: str = Field(
        description="The refresh token from the most recent token pair. Single use — refreshing "
        "rotates it and revokes every token issued before.",
    )


class ChangePasswordRequest(CamelModel):
    current_password: str = Field(
        description=(
            "Your current password. Required even though you are signed in: an access token can be "
            "borrowed, and knowing the password is the evidence that you are its owner."
        )
    )
    new_password: str = Field(
        min_length=PASSWORD_MIN_LENGTH,
        max_length=128,
        description=(
            f"At least {PASSWORD_MIN_LENGTH} characters, and different from the current one."
        ),
        examples=["a-much-better-passphrase"],
    )


class UserResponse(CamelModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    role: str
    tenant_id: uuid.UUID
    is_platform_admin: bool = Field(
        default=False,
        description="Platform staff. Granted by the deployment, never through this API.",
    )
    must_change_password: bool = Field(
        default=False,
        description=(
            "**True means the account can do nothing but change its password.** Set on an account "
            "whose password was chosen by somebody else — the administrator a deployment creates "
            "from its environment. Every other endpoint answers `403 PASSWORD_CHANGE_REQUIRED` "
            "until `POST /auth/password` has been called."
        ),
    )


class TenantResponse(CamelModel):
    id: uuid.UUID
    name: str
    plan: str


class TokenPairResponse(CamelModel):
    access_token: str = Field(description="Send as `Authorization: Bearer <token>`.")
    refresh_token: str = Field(description="Use once against `/auth/refresh`.")
    token_type: str = Field(default="bearer", description="Always `bearer`.")
    expires_at: datetime = Field(description="Expiry of the access token, UTC.")


class AuthenticatedResponse(CamelModel):
    user: UserResponse
    tokens: TokenPairResponse


class LogoutResponse(CamelModel):
    revoked: int = Field(description="Number of tokens invalidated by this call.")
