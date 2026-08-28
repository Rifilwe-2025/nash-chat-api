"""Current-user and current-tenant dependencies.

These live in the tenants module because *tenancy* is what they establish; token validation itself
is delegated to the auth module's service (service → service, never into its repositories).

Every protected route in every module depends on :data:`CurrentTenantDep` for its tenant id, and
passes it to a ``TenantScopedRepository``. That is the single path by which a request's data scope
is decided.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.modules.auth.domain.services import AuthenticatedUser, AuthService
from src.modules.tenants.domain.models import User
from src.shared.database.dependencies import SessionDep
from src.shared.exceptions import UnauthorizedException

bearer_scheme = HTTPBearer(auto_error=False, description="Access token from `/auth/login`.")

CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


async def get_authenticated(session: SessionDep, credentials: CredentialsDep) -> AuthenticatedUser:
    if credentials is None or not credentials.credentials:
        raise UnauthorizedException(
            "Provide an access token as 'Authorization: Bearer <token>'.",
            code="UNAUTHORIZED",
        )
    return await AuthService(session).authenticate(credentials.credentials)


AuthenticatedDep = Annotated[AuthenticatedUser, Depends(get_authenticated)]


async def get_current_user(authenticated: AuthenticatedDep) -> User:
    return authenticated.user


async def get_current_tenant_id(authenticated: AuthenticatedDep) -> uuid.UUID:
    return authenticated.tenant_id


CurrentUserDep = Annotated[User, Depends(get_current_user)]
CurrentTenantDep = Annotated[uuid.UUID, Depends(get_current_tenant_id)]
