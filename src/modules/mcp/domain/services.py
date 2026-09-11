"""Personal access tokens, and the platform as a coding agent sees it through MCP.

Two audiences, kept apart the way the API key service keeps its two apart:

* **Token management** is what a signed-in user does from the console: issue a token for their own
  coding agent, list their tokens, revoke one. It is scoped to the user as well as the tenant — a
  token is a personal credential, and one member of an organisation has no business listing or
  revoking another's.
* **:meth:`PersonalAccessTokenService.authenticate`** is what the MCP endpoint calls before any
  tenant is known. It is a classmethod taking a bare session so it can never be confused with a
  scoped read.

:class:`McpWorkspace` is the other half: once a token has been authenticated, it is the set of the
platform's own services, each constructed for the token's tenant. The MCP tools call those services
and nothing else — service to service, exactly as a router does — so every rule a module enforces
about its own rows (tenant scoping, validation, lifecycle) holds for a coding agent unchanged.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cached_property

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.agents.domain.services import AgentService
from src.modules.analytics.domain.services import AnalyticsService
from src.modules.api_keys.domain.services import ApiKeyService
from src.modules.channels.domain.services import ChannelService
from src.modules.conversations.domain.services import ConversationService
from src.modules.knowledge_base.domain.services import KnowledgeBaseService
from src.modules.mcp.domain.models import DEFAULT_SCOPES, McpScope, PersonalAccessToken
from src.modules.mcp.domain.repositories import PersonalAccessTokenRepository, authenticate
from src.modules.mcp.internal.token_generator import GeneratedToken, generate_token, hash_token
from src.modules.tenants.domain.services import TenantService
from src.modules.tools.domain.services import ResponseCache, ToolService
from src.shared.database.pagination import Page, PageRequest
from src.shared.exceptions import (
    ForbiddenException,
    NotFoundException,
    UnauthorizedException,
    ValidationException,
)
from src.shared.llm import LLMClient

logger = logging.getLogger("api.mcp")

#: How stale ``last_used_at`` may get before a request writes it again. An editor makes several
#: requests per tool call; stamping every one would be a write per request for a column whose whole
#: purpose is answering "is this token still in use?", which a minute's resolution answers fine.
LAST_USED_RESOLUTION = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class McpPrincipal:
    """Who a verified personal access token speaks for, and what it may do."""

    token_id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    scopes: frozenset[str]

    def allows(self, scope: McpScope) -> bool:
        """``mcp:write`` includes ``mcp:read`` — see :class:`McpScope`."""
        if scope is McpScope.READ and McpScope.WRITE.value in self.scopes:
            return True
        return scope.value in self.scopes


class PersonalAccessTokenService:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.tokens = PersonalAccessTokenRepository(session, tenant_id)

    # -- management ----------------------------------------------------------

    async def get(self, token_id: uuid.UUID) -> PersonalAccessToken:
        """One of the caller's own tokens. Somebody else's reads as missing, never as forbidden."""
        token = await self.tokens.get(token_id)
        if token is None or token.user_id != self.user_id:
            raise NotFoundException(
                "Personal access token does not exist.", code="PERSONAL_TOKEN_NOT_FOUND"
            )
        return token

    async def list_tokens(self, page: PageRequest) -> Page[PersonalAccessToken]:
        return await self.tokens.list_for_user(self.user_id, page)

    async def issue(
        self,
        name: str,
        scopes: list[str] | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[PersonalAccessToken, GeneratedToken]:
        """Create a token and return the secret **once** — it is not stored anywhere."""
        if expires_at is not None and expires_at <= datetime.now(UTC):
            raise ValidationException(
                "The expiry must be in the future.", code="PERSONAL_TOKEN_EXPIRY_IN_PAST"
            )

        generated = generate_token()
        token = await self.tokens.add(
            PersonalAccessToken(
                user_id=self.user_id,
                name=name.strip(),
                token_hash=generated.token_hash,
                prefix=generated.prefix,
                scopes=_normalised_scopes(scopes),
                expires_at=expires_at,
            )
        )
        logger.info("personal access token %s issued for user %s", token.id, self.user_id)
        return token, generated

    async def revoke(self, token_id: uuid.UUID) -> PersonalAccessToken:
        """Kill a token. Refused from its next request — nothing caches the decision."""
        token = await self.get(token_id)
        if token.revoked_at is not None:
            return token
        logger.info("personal access token %s revoked", token.id)
        return await self.tokens.update(token, revoked_at=datetime.now(UTC))

    # -- authentication ------------------------------------------------------

    @classmethod
    async def authenticate(cls, session: AsyncSession, secret: str) -> McpPrincipal:
        """Verify a presented secret and resolve who it speaks for.

        Checked on every request rather than cached, in the same order the sign-in path checks a
        user, so revoking a token, disabling an account, or forcing a password change all take
        effect on the very next call a coding agent makes:

        1. the token exists and is live — every failure here reads identically, because telling
           "no such token" from "revoked" hands out a map of the token space;
        2. its user still exists and still belongs to the tenant the token was issued in;
        3. the account is enabled (platform staff excepted, as at sign-in);
        4. the user is not waiting on a forced password change.
        """
        invalid = UnauthorizedException(
            "The personal access token is missing, invalid, revoked, or expired.",
            code="INVALID_PERSONAL_TOKEN",
        )

        token = await authenticate(session, hash_token(secret))
        if token is None or not token.is_active:
            raise invalid

        user = await TenantService(session).find_user(token.user_id)
        if user is None or user.tenant_id != token.tenant_id:
            raise invalid

        if not (user.is_platform_admin or user.tenant.is_active):
            raise ForbiddenException(
                "This account has been disabled. Contact support to have it restored.",
                code="ACCOUNT_DISABLED",
            )
        if user.must_change_password:
            raise ForbiddenException(
                "This account must change its password before it can be used.",
                code="PASSWORD_CHANGE_REQUIRED",
            )

        now = datetime.now(UTC)
        if token.last_used_at is None or now - token.last_used_at >= LAST_USED_RESOLUTION:
            # Straight through the session rather than a repository: no tenant-scoped repository
            # exists yet on this path, and the row is already loaded and verified.
            token.last_used_at = now
            await session.flush()

        return McpPrincipal(
            token_id=token.id,
            user_id=user.id,
            tenant_id=token.tenant_id,
            email=user.email,
            scopes=frozenset(str(scope) for scope in token.scopes or []),
        )


def _normalised_scopes(scopes: list[str] | None) -> list[str]:
    if scopes is None:
        return list(DEFAULT_SCOPES)

    known = {scope.value for scope in McpScope}
    unknown = sorted(scope for scope in scopes if scope not in known)
    if unknown:
        raise ValidationException(
            f"Unknown scopes: {', '.join(unknown)}. Supported: {', '.join(sorted(known))}.",
            code="UNKNOWN_SCOPE",
        )
    if not scopes:
        raise ValidationException(
            "A personal access token needs at least one scope.", code="VALIDATION_ERROR"
        )
    return sorted(set(scopes))


class McpWorkspace:
    """The platform's services, scoped to the tenant one personal access token speaks for.

    Built once per tool call, around that call's own transaction. Each service is constructed on
    first use, so a tool that only lists agents does not assemble a conversation engine it will
    never run.

    The tenant comes from the verified token and from nowhere else — not from a tool argument, and
    not from the ``X-Tenant-Id`` header platform staff use on the REST API. A coding agent acts in
    its owner's own organisation, full stop.
    """

    def __init__(
        self,
        session: AsyncSession,
        principal: McpPrincipal,
        tool_cache: ResponseCache | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.session = session
        self.principal = principal
        self.tenant_id = principal.tenant_id
        self._tool_cache = tool_cache
        self._llm_client = llm_client

    @cached_property
    def accounts(self) -> TenantService:
        return TenantService(self.session)

    @cached_property
    def agents(self) -> AgentService:
        return AgentService(self.session, self.tenant_id)

    @cached_property
    def knowledge(self) -> KnowledgeBaseService:
        return KnowledgeBaseService(self.session, self.tenant_id)

    @cached_property
    def tools(self) -> ToolService:
        return ToolService(self.session, self.tenant_id, cache=self._tool_cache)

    @cached_property
    def conversations(self) -> ConversationService:
        return ConversationService(
            self.session,
            self.tenant_id,
            llm_client=self._llm_client,
            tool_cache=self._tool_cache,
        )

    @cached_property
    def channels(self) -> ChannelService:
        return ChannelService(self.session, self.tenant_id)

    @cached_property
    def api_keys(self) -> ApiKeyService:
        return ApiKeyService(self.session, self.tenant_id)

    @cached_property
    def analytics(self) -> AnalyticsService:
        return AnalyticsService(self.session, self.tenant_id)
