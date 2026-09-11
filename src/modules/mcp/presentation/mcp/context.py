"""The request a tool call belongs to, and the one way a tool reaches the platform.

A tool is not called by FastAPI. The MCP runtime dispatches it, several frames below the HTTP
request that carried it, with none of the dependency injection a route gets. So the endpoint that
authenticated the request binds what it established — who the token speaks for, where the database
is — to a context variable, and every tool reads it back through :func:`workspace`.

:func:`workspace` is where the rules a route gets for free are applied by hand, in one place:

* **the scope check**, before anything is read;
* **one transaction per tool call**, committed when the tool returns and rolled back when it raises,
  exactly as a request's session is;
* **error translation** — an ``AppException`` becomes a tool error carrying its stable code, so a
  coding agent reads ``AGENT_NOT_FOUND: …`` and can act on it, rather than an opaque failure.
"""

from __future__ import annotations

import contextvars
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from src.modules.mcp.domain.models import McpScope
from src.modules.mcp.domain.services import McpPrincipal, McpWorkspace
from src.modules.tools.domain.services import ResponseCache
from src.shared.database.unit_of_work import SessionFactory, session_scope
from src.shared.exceptions import AppException, ForbiddenException
from src.shared.llm import LLMClient


@dataclass(frozen=True, slots=True)
class McpRequest:
    """What the endpoint established about one authenticated MCP request."""

    principal: McpPrincipal
    session_factory: SessionFactory
    #: The origin this API is reachable at from outside, for anything that writes a URL down.
    base_url: str
    #: The schema of the API serving this request, for the integration guide.
    openapi: Callable[[], dict[str, Any]]
    tool_cache: ResponseCache | None = None
    #: A provider client to use instead of the real one. Only the tests set it.
    llm_client: LLMClient | None = None


_current: contextvars.ContextVar[McpRequest | None] = contextvars.ContextVar(
    "mcp_request", default=None
)


def bind(request: McpRequest) -> contextvars.Token[McpRequest | None]:
    return _current.set(request)


def unbind(token: contextvars.Token[McpRequest | None]) -> None:
    _current.reset(token)


def current_request() -> McpRequest:
    request = _current.get()
    if request is None:
        # Only reachable by calling a tool outside the authenticated endpoint — a wiring mistake,
        # reported as a refusal rather than a crash so nothing is ever served unauthenticated.
        raise ToolError("UNAUTHORIZED: this tool is only available through the /mcp endpoint.")
    return request


def describe(exc: AppException) -> str:
    """``CODE: detail`` — the stable code first, because that is what a caller branches on."""
    return f"{exc.code}: {exc.detail or exc.message}"


@asynccontextmanager
async def workspace(scope: McpScope) -> AsyncIterator[McpWorkspace]:
    """The platform's services for this call's tenant, inside this call's own transaction."""
    request = current_request()
    try:
        if not request.principal.allows(scope):
            raise ForbiddenException(
                f"This personal access token does not carry the {scope.value!r} scope. Issue one "
                "with it from the console's developer settings.",
                code="INSUFFICIENT_SCOPE",
            )
        async with session_scope(request.session_factory) as session:
            yield McpWorkspace(
                session,
                request.principal,
                tool_cache=request.tool_cache,
                llm_client=request.llm_client,
            )
    except AppException as exc:
        raise ToolError(describe(exc)) from exc
