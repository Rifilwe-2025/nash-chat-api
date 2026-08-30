"""API key issue, scoping, revocation, and authentication (spec §5.6).

Two audiences use this service and they are kept apart on purpose:

* **Tenant-scoped methods** are what a signed-in user calls to manage their keys. They take the
  tenant from the token like every other module.
* **:meth:`authenticate`** is what the public chat API calls, and it runs *before* any tenant is
  known — it is the thing that establishes one. It is a classmethod taking a bare session so it can
  never be confused with a scoped read.

A revoked key is rejected on the very next request: authentication reads the row every time rather
than caching a decision. That is the phase's bar, and it is why revocation is a column rather than
a delete — the row must still be there to say "no".
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src import configs
from src.modules.agents.domain.models import Agent, AgentStatus
from src.modules.agents.domain.services import AgentService
from src.modules.api_keys.domain.models import DEFAULT_SCOPES, ApiKey, ApiKeyScope
from src.modules.api_keys.domain.repositories import ApiKeyRepository, authenticate
from src.modules.api_keys.internal.key_generator import GeneratedKey, generate_key, hash_key
from src.modules.tenants.domain.services import TenantService
from src.shared.database.pagination import Page, PageRequest
from src.shared.exceptions import (
    ForbiddenException,
    NotFoundException,
    UnauthorizedException,
    ValidationException,
)

logger = logging.getLogger("api.api_keys")


class AuthenticatedKey:
    """A verified key and the agent it speaks for."""

    def __init__(self, api_key: ApiKey, agent: Agent) -> None:
        self.api_key = api_key
        self.agent = agent
        self.tenant_id = api_key.tenant_id


class ApiKeyService:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.keys = ApiKeyRepository(session, tenant_id)
        self.agents = AgentService(session, tenant_id)

    # -- management ----------------------------------------------------------

    async def get(self, key_id: uuid.UUID) -> ApiKey:
        api_key = await self.keys.get(key_id)
        if api_key is None:
            raise NotFoundException("API key does not exist.", code="API_KEY_NOT_FOUND")
        return api_key

    async def list_keys(self, page: PageRequest, agent_id: uuid.UUID | None = None) -> Page[ApiKey]:
        if agent_id is None:
            return await self.keys.list(page)
        await self.agents.get(agent_id)  # 404s a foreign agent before listing anything
        return await self.keys.list_for_agent(agent_id, page)

    async def issue(
        self,
        agent_id: uuid.UUID,
        name: str,
        scopes: list[str] | None = None,
        rate_limit_per_minute: int | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[ApiKey, GeneratedKey]:
        """Create a key and return the secret **once**.

        The caller must hand the secret straight to the user: it is not stored, so this return
        value is the only time it exists anywhere.
        """
        agent = await self.agents.get(agent_id)
        selected = self._validate_scopes(scopes)
        limit = self._validate_rate_limit(rate_limit_per_minute)

        if expires_at is not None and expires_at <= datetime.now(UTC):
            raise ValidationException(
                "The expiry must be in the future.", code="API_KEY_EXPIRY_IN_PAST"
            )

        generated = generate_key()
        api_key = await self.keys.add(
            ApiKey(
                agent_id=agent.id,
                name=name,
                key_hash=generated.key_hash,
                prefix=generated.prefix,
                scopes=selected,
                rate_limit_per_minute=limit,
                expires_at=expires_at,
            )
        )
        logger.info("api key %s issued for agent %s", api_key.id, agent.id)
        return api_key, generated

    async def update(
        self,
        key_id: uuid.UUID,
        name: str | None = None,
        scopes: list[str] | None = None,
        rate_limit_per_minute: int | None = None,
    ) -> ApiKey:
        """Change what a key may do without reissuing it."""
        api_key = await self.get(key_id)

        changes: dict[str, object] = {}
        if name is not None:
            changes["name"] = name
        if scopes is not None:
            changes["scopes"] = self._validate_scopes(scopes)
        if rate_limit_per_minute is not None:
            changes["rate_limit_per_minute"] = self._validate_rate_limit(rate_limit_per_minute)

        if not changes:
            return api_key
        return await self.keys.update(api_key, **changes)

    async def revoke(self, key_id: uuid.UUID) -> ApiKey:
        """Kill a key. Effective on its next request — nothing caches the decision."""
        api_key = await self.get(key_id)
        if api_key.revoked_at is not None:
            return api_key
        logger.info("api key %s revoked", api_key.id)
        return await self.keys.update(api_key, revoked_at=datetime.now(UTC))

    # -- authentication ------------------------------------------------------

    @classmethod
    async def authenticate(cls, session: AsyncSession, secret: str) -> AuthenticatedKey:
        """Verify a presented secret and resolve the agent it speaks for.

        Every failure returns the same message. A caller holding a bad key must not be able to tell
        "no such key" from "revoked" from "expired" — the differences are a map of the key space.
        The tenant is told which of their keys are revoked through the key list; a stranger is not.
        """
        api_key = await authenticate(session, hash_key(secret))
        if api_key is None or not api_key.is_active:
            raise UnauthorizedException(
                "The API key is missing, invalid, or revoked.", code="INVALID_API_KEY"
            )

        # The account itself, before the agent: a disabled tenant's agents stop answering on every
        # channel, and a key is the one credential that never passes through the user sign-in path
        # where that is otherwise enforced.
        tenant = await TenantService(session).get_tenant(api_key.tenant_id)
        if not tenant.is_active:
            raise ForbiddenException(
                "This account is not currently active.", code="ACCOUNT_DISABLED"
            )

        agent = await AgentService(session, api_key.tenant_id).get(api_key.agent_id)
        if agent.status is not AgentStatus.PUBLISHED:
            raise ForbiddenException(
                "This agent is not currently serving traffic.", code="AGENT_NOT_PUBLISHED"
            )

        return AuthenticatedKey(api_key, agent)

    @staticmethod
    def require_scope(authenticated: AuthenticatedKey, scope: ApiKeyScope) -> None:
        if not authenticated.api_key.allows(scope):
            raise ForbiddenException(
                f"This API key does not carry the {scope.value!r} scope.",
                code="INSUFFICIENT_SCOPE",
            )

    @staticmethod
    async def record_use(session: AsyncSession, api_key: ApiKey) -> None:
        """Stamp the key as used.

        Written straight through the session rather than a repository: this runs on the public
        chat path where no tenant-scoped repository has been constructed, and the row is already
        loaded and verified.
        """
        api_key.last_used_at = datetime.now(UTC)
        await session.flush()

    # -- validation ----------------------------------------------------------

    def _validate_scopes(self, scopes: list[str] | None) -> list[str]:
        if scopes is None:
            return list(DEFAULT_SCOPES)

        known = {scope.value for scope in ApiKeyScope}
        unknown = [scope for scope in scopes if scope not in known]
        if unknown:
            raise ValidationException(
                f"Unknown scopes: {', '.join(sorted(unknown))}. Supported: "
                f"{', '.join(sorted(known))}.",
                code="UNKNOWN_SCOPE",
            )
        if not scopes:
            raise ValidationException("A key needs at least one scope.", code="API_KEY_NEEDS_SCOPE")
        return sorted(set(scopes))

    def _validate_rate_limit(self, requested: int | None) -> int:
        default: int = configs.RATE_LIMIT_DEFAULT_PER_MINUTE
        if requested is None:
            return default

        maximum: int = configs.RATE_LIMIT_MAX_PER_MINUTE
        if requested < 1 or requested > maximum:
            raise ValidationException(
                f"The rate limit must be between 1 and {maximum} requests per minute.",
                code="INVALID_RATE_LIMIT",
            )
        return requested
