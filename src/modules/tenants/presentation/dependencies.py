"""Current-user and current-tenant dependencies.

These live in the tenants module because *tenancy* is what they establish; token validation itself
is delegated to the auth module's service (service → service, never into its repositories).

Every protected route in every module depends on :data:`CurrentTenantDep` for its tenant id, and
passes it to a ``TenantScopedRepository``. That is the single path by which a request's data scope
is decided — which is what makes it the right place, and the only place, to let platform staff act
on another tenant's behalf.

**How an admin gets CRUD over everything without a second API.** A platform admin sends
``X-Tenant-Id``; this dependency returns that tenant instead of their own, and every existing
endpoint then works unchanged — creating an agent, uploading knowledge, reading a transcript. No
admin-only mirror of each module's routes, and no unscoped query anywhere: the repositories are
still tenant-scoped, the admin has simply chosen which tenant they are scoped to. One function
decides it, so there is one thing to audit rather than a hundred.

**For everybody else the header does nothing at all.** It is ignored rather than refused: refusing
would confirm which tenant ids exist, and a header any caller can set must never be able to widen a
caller's scope. Every use of it *is* logged, admin or not — one line for the audit trail, one line
because a non-admin sending it is worth knowing about.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.modules.auth.domain.services import AuthenticatedUser, AuthService
from src.modules.tenants.domain.models import User
from src.modules.tenants.domain.services import TenantService
from src.shared.database.dependencies import SessionDep
from src.shared.exceptions import UnauthorizedException

logger = logging.getLogger("api.tenants.scope")

bearer_scheme = HTTPBearer(auto_error=False, description="Access token from `/auth/login`.")

CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]

ActingAsDep = Annotated[
    uuid.UUID | None,
    Header(
        alias="X-Tenant-Id",
        description=(
            "Platform administrators only: act on this tenant for the duration of the request, as "
            "though signed in to it. Ignored for everyone else."
        ),
    ),
]


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


async def get_current_tenant_id(
    authenticated: AuthenticatedDep,
    session: SessionDep,
    acting_as: ActingAsDep = None,
) -> uuid.UUID:
    """The tenant this request is scoped to.

    The caller's own tenant, unless they are platform staff naming another one — in which case the
    tenant must exist, which is checked through the tenants service so a bad id is the same 404 it
    is everywhere else.
    """
    if acting_as is None or acting_as == authenticated.tenant_id:
        return authenticated.tenant_id

    if not authenticated.user.is_platform_admin:
        logger.warning(
            "user %s sent X-Tenant-Id %s without platform admin; scoping to their own tenant",
            authenticated.user.id,
            acting_as,
        )
        return authenticated.tenant_id

    # Raises TENANT_NOT_FOUND for an id that does not exist. A disabled tenant is deliberately
    # allowed: administering an account is most needed when it is switched off.
    await TenantService(session).get_tenant(acting_as)
    logger.warning("platform admin %s acting as tenant %s", authenticated.user.id, acting_as)
    return acting_as


CurrentUserDep = Annotated[User, Depends(get_current_user)]
CurrentTenantDep = Annotated[uuid.UUID, Depends(get_current_tenant_id)]
