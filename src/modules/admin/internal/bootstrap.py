"""Creating the first platform administrator from configuration.

A deployment starts with an empty database and nobody who can administer it, which is a chicken and
egg problem: the flag that makes somebody staff is granted by a script that needs a user to grant it
to, and there is no way to become the first one. This closes that loop from the environment, the one
place a fresh deployment already has to be configured.

**The password in the environment is a handover, not a credential.** It sits in a file every person
with deploy access can read, it is usually the same string across environments, and it will be in
somebody's shell history within a week. So the account is created with ``must_change_password``
already set: it can sign in and change its password, and nothing else, until it has. Until then the
API says so on every boot.

Three rules keep this from becoming a way to lose an account:

* **It only ever creates.** If a user with that address exists, nothing is written — no password
  reset, no re-flagging. A deployment restarting must never hand a live account a password from an
  environment file, and an administrator who has changed theirs must not have it changed back at
  three in the morning by a container restart.
* **It needs both values.** Without an email *and* a password nothing happens at all. A half-set
  environment creates no account and no default password.
* **It is quiet about the password and loud about everything else.** The address and the tenant are
  logged; the password never is.
* **It refuses an address that cannot sign in.** ``/auth/login`` validates the email, and rejects
  special-use domains such as ``.local`` and ``.test``. Creating an account with one would produce
  an administrator nobody can authenticate as — a failure that only shows up at the worst moment, so
  it is caught here instead.

Run at startup rather than as a migration: a migration is about schema, applies once per database
and cannot read the environment of the process that will serve traffic. Startup can, and being
idempotent it costs one indexed lookup per boot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError
from pydantic.networks import EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from src import configs
from src.modules.auth.domain.services import AuthService
from src.modules.tenants.domain.models import User
from src.modules.tenants.domain.services import TenantService

logger = logging.getLogger("api.admin.bootstrap")

# Below this the password is not worth the round trip: the account exists to be handed over, and a
# handover password short enough to guess is worse than no bootstrap account at all.
MIN_PASSWORD_LENGTH = 12

# The same validation the login endpoint applies, so an address that cannot sign in never becomes
# an account. It rejects special-use domains — `.local`, `.test`, `.invalid` — which are exactly
# what somebody reaches for when inventing an internal address.
_EMAIL = TypeAdapter(EmailStr)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """What happened, so the caller can log it without re-deriving it."""

    created: bool
    reason: str
    email: str | None = None


async def ensure_bootstrap_admin(session: AsyncSession) -> BootstrapResult:
    """Create the configured platform administrator if it does not exist yet.

    Never raises. A misconfigured bootstrap must not stop the API from serving — a deployment with
    no administrator is a deployment somebody has to fix, while a deployment that will not start is
    an outage.
    """
    email: str = (configs.ADMIN_BOOTSTRAP_EMAIL or "").strip()
    password: str = configs.ADMIN_BOOTSTRAP_PASSWORD or ""

    if not email or not password:
        return BootstrapResult(created=False, reason="not configured")

    try:
        _EMAIL.validate_python(email)
    except ValidationError:
        logger.error(
            "ADMIN_BOOTSTRAP_EMAIL %r is not an address that can sign in — special-use domains "
            "such as .local and .test are rejected by the login endpoint; no administrator was "
            "created",
            email,
        )
        return BootstrapResult(created=False, reason="email invalid", email=email)

    if len(password) < MIN_PASSWORD_LENGTH:
        logger.error(
            "ADMIN_BOOTSTRAP_PASSWORD is shorter than %d characters; no administrator was created",
            MIN_PASSWORD_LENGTH,
        )
        return BootstrapResult(created=False, reason="password too short", email=email)

    try:
        existing = await TenantService(session).find_by_email(email)
        if existing is not None:
            # Deliberately does nothing else. See the module docstring: a restart must not reset a
            # live account's password back to what is in the environment.
            return BootstrapResult(created=False, reason="already exists", email=email)

        # Through the auth service: passwords are hashed in one place, and this is not it.
        await AuthService(session).provision_account(
            email=email,
            password=password,
            tenant_name=configs.ADMIN_BOOTSTRAP_TENANT_NAME or "Platform",
            full_name=configs.ADMIN_BOOTSTRAP_NAME or None,
            is_platform_admin=True,
            must_change_password=True,
        )
        await session.commit()

        logger.warning(
            "created the bootstrap platform administrator %s — it must change its password before "
            "it can do anything else",
            email,
        )
        return BootstrapResult(created=True, reason="created", email=email)
    except Exception:
        await session.rollback()
        logger.exception("could not create the bootstrap administrator")
        return BootstrapResult(created=False, reason="failed", email=email)


async def warn_if_handover_password_unchanged(session: AsyncSession) -> None:
    """Say so on every boot while an administrator is still on its handover password.

    The warning is the whole reason the flag is worth having on a column rather than in somebody's
    memory: the risk is not that the password is weak, it is that it is *shared*, and shared
    credentials are forgotten rather than discovered.
    """
    email: str = (configs.ADMIN_BOOTSTRAP_EMAIL or "").strip()
    if not email:
        return

    user: User | None = await TenantService(session).find_by_email(email)
    if user is not None and user.must_change_password:
        logger.warning(
            "the platform administrator %s is still using the password from the environment; "
            "sign in and change it — until then that account can do nothing else",
            email,
        )
