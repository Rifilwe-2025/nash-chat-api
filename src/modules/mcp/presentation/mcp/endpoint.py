"""The ``/mcp`` endpoint: authenticate, rate limit, then hand the request to the MCP runtime.

**Why a route of our own rather than the SDK's Starlette app.** The SDK can build an app with its
own bearer-token middleware, but mounting that app would put ``/mcp`` behind a ``Mount`` — which
answers a ``POST /mcp`` with a redirect to ``/mcp/`` that most MCP clients will not follow — and
would authenticate with a verifier that can only say yes or no. This endpoint is registered on the
FastAPI application directly, so it sits behind the same middleware as every other route (request
id, security headers, logging), and refuses with the platform's own envelope and error codes:
``INVALID_PERSONAL_TOKEN``, ``ACCOUNT_DISABLED``, ``RATE_LIMITED``.

**Stateless on purpose.** The API runs as several workers behind a proxy. A stateful MCP session is
held in one worker's memory, and the next request for it may well land on another worker that has
never heard of it. In stateless JSON mode every request stands alone, carries its own token, and is
authenticated from scratch — which is also what makes a revoked token stop on the very next call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from src import configs
from src.core.public_url import public_base_url
from src.core.rate_limit import RateLimiter, build_limiter
from src.modules.mcp.domain.services import PersonalAccessTokenService
from src.modules.mcp.presentation.mcp.context import McpRequest, bind, unbind
from src.shared.database.unit_of_work import session_scope
from src.shared.exceptions import AppException, RateLimitedException, UnauthorizedException

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

logger = logging.getLogger("api.mcp")

MCP_PATH = "/mcp"


def bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()


async def refuse(
    scope: Scope, receive: Receive, send: Send, exc: AppException, headers: dict[str, str]
) -> None:
    """The platform's failure envelope, with the status its exception carries."""
    body: dict[str, Any] = {"success": False, "error": {"code": exc.code}, "message": exc.message}
    if exc.detail:
        body["error"]["detail"] = exc.detail
    status = int(exc.status_code)
    if status == 401:
        # What an OAuth-aware MCP client reads to learn that the credential, not the request, is
        # the problem.
        headers = {
            **headers,
            "WWW-Authenticate": f'Bearer realm="mcp", error="invalid_token", '
            f'error_description="{exc.code}"',
        }
    await JSONResponse(body, status_code=status, headers=headers)(scope, receive, send)


class McpEndpoint:
    """ASGI application in front of the MCP session manager."""

    def __init__(self, server: MCPServer[Any]) -> None:
        self._server = server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - the route is only ever reached over HTTP
            return

        request = Request(scope, receive)
        state = request.app.state

        secret = bearer_token(request.headers.get("authorization"))
        if secret is None:
            await refuse(
                scope,
                receive,
                send,
                UnauthorizedException(
                    "Provide a personal access token as 'Authorization: Bearer <token>'. Issue one "
                    "from the console's developer settings.",
                    code="UNAUTHORIZED",
                ),
                {},
            )
            return

        try:
            async with session_scope(state.session_factory) as session:
                principal = await PersonalAccessTokenService.authenticate(session, secret)
        except AppException as exc:
            await refuse(scope, receive, send, exc, {})
            return

        limiter: RateLimiter | None = getattr(state, "rate_limiter", None)
        if limiter is None:  # pragma: no cover - lifespan always sets this
            limiter = build_limiter()
            state.rate_limiter = limiter

        # Counted per token and after authentication, like the chat API's per-key limit: an
        # unauthenticated flood cannot spend a real token's allowance.
        limit: int = configs.MCP_RATE_LIMIT_PER_MINUTE
        verdict = await limiter.check(f"mcp:{principal.token_id}", limit)
        headers = verdict.headers()
        if not verdict.allowed:
            await refuse(
                scope,
                receive,
                send,
                RateLimitedException(
                    f"Rate limit of {verdict.limit} requests per minute exceeded. "
                    f"Retry in {verdict.retry_after} seconds.",
                    code="RATE_LIMITED",
                ),
                headers,
            )
            return
        # Copied onto the response by the request middleware, as for every limited route.
        scope.setdefault("state", {})["rate_limit_headers"] = headers

        token = bind(
            McpRequest(
                principal=principal,
                session_factory=state.session_factory,
                base_url=public_base_url(request),
                openapi=request.app.openapi,
                tool_cache=getattr(state, "tool_cache", None),
                llm_client=getattr(state, "mcp_llm_client", None),
            )
        )
        try:
            await self._server.session_manager.handle_request(scope, receive, send)
        finally:
            unbind(token)
