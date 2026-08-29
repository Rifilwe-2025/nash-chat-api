"""Authenticating the public chat API by key, and rate limiting it (spec §5.6).

This is the only place in the codebase where a request's tenant comes from something other than a
user's access token. The key *is* the tenant, the agent, and the permission set, all resolved in one
step — which is why the checks are ordered deliberately:

1. **The key is presented** — as ``Authorization: Bearer`` or ``X-API-Key``, because integrations
   will reach for one or the other and refusing the wrong one teaches nothing.
2. **The key is valid, live, and its agent is published.** Every failure here reads identically to
   a caller: distinguishing "no such key" from "revoked" hands out a map of the key space.
3. **The rate limit is counted** — after authentication, so an unauthenticated flood cannot consume
   a real key's allowance, and per key rather than per IP, because that is what a tenant is sold.
4. **The scope is checked** by the route, since it differs per route.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.core.rate_limit import RateLimiter, build_limiter
from src.modules.api_keys.domain.models import ApiKeyScope
from src.modules.api_keys.domain.services import ApiKeyService, AuthenticatedKey
from src.modules.channels.domain.services import ChannelService
from src.modules.conversations.domain.services import ConversationService
from src.shared.database.dependencies import SessionDep
from src.shared.exceptions import RateLimitedException, UnauthorizedException

api_key_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="ApiKey",
    description="Agent API key from `/agents/{agentId}/api-keys`.",
)

CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(api_key_scheme)]
HeaderKeyDep = Annotated[
    str | None, Header(alias="X-API-Key", description="Alternative to the bearer header.")
]


def get_rate_limiter(request: Request) -> RateLimiter:
    """One limiter per process, built at startup and pinned to ``app.state``."""
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:  # pragma: no cover - lifespan always sets this
        limiter = build_limiter()
        request.app.state.rate_limiter = limiter
    return limiter


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


async def get_api_caller(
    request: Request,
    session: SessionDep,
    limiter: RateLimiterDep,
    credentials: CredentialsDep,
    header_key: HeaderKeyDep = None,
) -> AuthenticatedKey:
    """Resolve and rate limit the caller behind an API key."""
    presented = (credentials.credentials if credentials else None) or header_key
    if not presented:
        raise UnauthorizedException(
            "Provide your API key as 'Authorization: Bearer <key>' or 'X-API-Key: <key>'.",
            code="MISSING_API_KEY",
        )

    authenticated = await ApiKeyService.authenticate(session, presented)

    verdict = await limiter.check(
        f"apikey:{authenticated.api_key.id}",
        authenticated.api_key.rate_limit_per_minute,
    )
    # Stashed for the middleware to copy onto the response: a client should learn its remaining
    # allowance from a successful call, not only from being refused.
    request.state.rate_limit_headers = verdict.headers()

    if not verdict.allowed:
        raise RateLimitedException(
            f"Rate limit of {verdict.limit} requests per minute exceeded. "
            f"Retry in {verdict.retry_after} seconds.",
            code="RATE_LIMITED",
        )

    await ApiKeyService.record_use(session, authenticated.api_key)
    return authenticated


ApiCallerDep = Annotated[AuthenticatedKey, Depends(get_api_caller)]


def require_chat_write(caller: ApiCallerDep) -> AuthenticatedKey:
    ApiKeyService.require_scope(caller, ApiKeyScope.CHAT_WRITE)
    return caller


def require_chat_read(caller: ApiCallerDep) -> AuthenticatedKey:
    ApiKeyService.require_scope(caller, ApiKeyScope.CHAT_READ)
    return caller


ChatWriteDep = Annotated[AuthenticatedKey, Depends(require_chat_write)]
ChatReadDep = Annotated[AuthenticatedKey, Depends(require_chat_read)]


def get_chat_conversations(session: SessionDep, caller: ApiCallerDep) -> ConversationService:
    """The conversation engine, scoped to the tenant the key belongs to.

    A dependency rather than a constructor call inside each route: the tenant is derived from the
    key in exactly one place, and tests can substitute a provider without patching anything.
    """
    return ConversationService(session, caller.tenant_id)


def get_chat_channels(session: SessionDep, caller: ApiCallerDep) -> ChannelService:
    return ChannelService(session, caller.tenant_id)


ChatConversationsDep = Annotated[ConversationService, Depends(get_chat_conversations)]
ChatChannelsDep = Annotated[ChannelService, Depends(get_chat_channels)]
