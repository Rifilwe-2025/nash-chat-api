"""Who is platform staff.

One dependency, and every route in this module carries it. It reuses the ordinary access token —
an admin signs in like anybody else, because they *are* a user of some tenant — and then checks the
one flag that is not reachable through any endpoint.

**The flag is granted out of band**, by `scripts/grant_platform_admin.py`. There is deliberately no
API for it: an endpoint that grants platform admin is an endpoint that escalates to platform admin,
and the only thing standing between a compromised tenant account and every other tenant's data would
be whichever check that endpoint happened to make.

A non-admin gets ``403`` rather than ``404``. The usual rule in this codebase is the opposite — a
resource in another tenant is a 404, so its existence is not confirmed — but that rule is about
*data*, and these routes hold none. That `/admin` exists is not a secret; who may use it is.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends

from src.modules.admin.domain.services import AdminService
from src.modules.tenants.domain.models import User
from src.modules.tenants.presentation.dependencies import CurrentUserDep
from src.shared.database.dependencies import SessionDep
from src.shared.exceptions import ForbiddenException

logger = logging.getLogger("api.admin")


def require_platform_admin(user: CurrentUserDep) -> User:
    if not user.is_platform_admin:
        logger.warning("user %s attempted a platform admin route", user.id)
        raise ForbiddenException(
            "This endpoint is for platform administrators.", code="PLATFORM_ADMIN_REQUIRED"
        )
    return user


PlatformAdminDep = Annotated[User, Depends(require_platform_admin)]


def get_admin_service(session: SessionDep, admin: PlatformAdminDep) -> AdminService:
    """The service, built only after the caller has been checked.

    Taking the admin as a parameter it does not use is the point: it makes the check part of
    constructing the service, so a route cannot reach the service without having passed it.
    """
    return AdminService(session)


AdminServiceDep = Annotated[AdminService, Depends(get_admin_service)]
