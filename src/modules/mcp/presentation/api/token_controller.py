from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Path, Request

from src import configs
from src.core.public_url import public_base_url
from src.modules.mcp.domain.models import McpScope, PersonalAccessToken
from src.modules.mcp.domain.services import PersonalAccessTokenService
from src.modules.mcp.presentation.dtos.token import (
    IssuedPersonalTokenResponse,
    IssuePersonalTokenRequest,
    McpConnectionResponse,
    McpScopeResponse,
    PersonalTokenResponse,
)
from src.modules.mcp.presentation.mcp import MCP_PATH
from src.modules.tenants.presentation.dependencies import CurrentUserDep
from src.shared.database.dependencies import SessionDep
from src.shared.database.pagination import PageParamsDep
from src.shared.responses import ApiResponse, PaginatedResponse, create_router

router = create_router(prefix="/mcp-tokens", tags=["mcp"])

SCOPE_DESCRIPTIONS: dict[McpScope, str] = {
    McpScope.READ: (
        "Read agents and their versions, knowledge bases and extracted source text, tools and "
        "their call logs, conversations, integration guides, API key metadata and analytics."
    ),
    McpScope.WRITE: (
        "Everything mcp:read allows, plus creating and configuring agents, knowledge bases, "
        "sources, tools and allowed origins, publishing and pausing agents, and running preview "
        "chats and tool test calls."
    ),
}


def get_token_service(session: SessionDep, user: CurrentUserDep) -> PersonalAccessTokenService:
    """The caller's **own** user and tenant — never the tenant an administrator is acting as.

    Every other module scopes by ``CurrentTenantDep``, which honours a platform admin's
    ``X-Tenant-Id``. That is right for an admin reading or fixing an account in the moment, and
    wrong here: a token outlives the request, so issuing one "as" another tenant would leave a
    standing credential inside somebody else's organisation. Tokens are therefore always issued in
    the tenant the user actually belongs to.
    """
    return PersonalAccessTokenService(session, user.tenant_id, user.id)


ServiceDep = Annotated[PersonalAccessTokenService, Depends(get_token_service)]
TokenIdPath = Annotated[uuid.UUID, Path(description="Identifier of the personal access token.")]

UNAUTHORIZED = {
    "description": "Access token is missing, invalid, or revoked (`UNAUTHORIZED`, `INVALID_TOKEN`)."
}
NOT_FOUND = {
    "description": (
        "No such token on your account (`PERSONAL_TOKEN_NOT_FOUND`). A token belonging to anybody "
        "else — in your organisation or another — is reported as missing rather than forbidden."
    )
}


def _token(token: PersonalAccessToken) -> PersonalTokenResponse:
    return PersonalTokenResponse(
        id=token.id,
        name=token.name,
        prefix=token.prefix,
        scopes=[str(scope) for scope in token.scopes],
        last_used_at=token.last_used_at,
        revoked_at=token.revoked_at,
        expires_at=token.expires_at,
        active=token.is_active,
        created_at=token.created_at,
    )


@router.get(
    "/connection",
    response_model=ApiResponse[McpConnectionResponse],
    summary="Get MCP connection details",
    description=(
        "Returns what a coding agent's MCP configuration needs besides the token: the endpoint "
        "URL, the transport, the header the token travels in, the scopes a token can carry, and "
        "the per-token rate limit.\n\n"
        "The URL is built from `PUBLIC_BASE_URL`, so behind a proxy it is the address a "
        "developer's machine can reach. `enabled` is false when the deployment has switched the "
        "endpoint off (`MCP_ENABLED=false`); tokens can still be listed and revoked."
    ),
    responses={200: {"description": "The connection details."}, 401: UNAUTHORIZED},
)
async def get_connection(request: Request, _: CurrentUserDep) -> ApiResponse[McpConnectionResponse]:
    """Declared ahead of `/{token_id}` so `connection` is read as a literal segment."""
    return ApiResponse.ok(
        McpConnectionResponse(
            enabled=configs.MCP_ENABLED,
            server_url=f"{public_base_url(request)}{MCP_PATH}",
            transport="streamable-http",
            auth_header="Authorization: Bearer <token>",
            scopes=[
                McpScopeResponse(scope=scope, description=SCOPE_DESCRIPTIONS[scope])
                for scope in McpScope
            ],
            rate_limit_per_minute=configs.MCP_RATE_LIMIT_PER_MINUTE,
        )
    )


@router.post(
    "",
    response_model=ApiResponse[IssuedPersonalTokenResponse],
    status_code=201,
    summary="Issue a personal access token",
    description=(
        "Creates a token for **your own** coding agent to reach the MCP endpoint, and **returns "
        "the secret once**. Only a hash is stored, so this response is the only time the token "
        "exists anywhere — copy it into your agent's configuration now. If it is lost, issue a new "
        "one and revoke this one.\n\n"
        "The token acts as you, inside your own organisation, with the scopes you give it: "
        "`mcp:read` (the default) to inspect, `mcp:write` to create and change. Platform "
        "administrators acting as another tenant through `X-Tenant-Id` still receive a token for "
        "their own organisation — a standing credential is never issued inside somebody else's."
    ),
    responses={
        201: {"description": "The token was issued. The secret is in `value.token`."},
        401: UNAUTHORIZED,
        422: {
            "description": (
                "An unknown scope or no scopes at all (`VALIDATION_ERROR`), or an expiry in the "
                "past (`PERSONAL_TOKEN_EXPIRY_IN_PAST`)."
            )
        },
    },
)
async def issue_token(
    payload: IssuePersonalTokenRequest, service: ServiceDep
) -> ApiResponse[IssuedPersonalTokenResponse]:
    token, generated = await service.issue(
        name=payload.name,
        scopes=[scope.value for scope in payload.scopes] if payload.scopes else None,
        expires_at=payload.expires_at,
    )
    return ApiResponse.ok(
        IssuedPersonalTokenResponse(token=generated.secret, personal_token=_token(token)),
        message="Store this token now — it will not be shown again.",
    )


@router.get(
    "",
    response_model=PaginatedResponse[PersonalTokenResponse],
    summary="List your personal access tokens",
    description=(
        "Lists the tokens **you** have issued, newest first, with their scopes, expiry and when a "
        "coding agent last used each one. Other members' tokens are never listed. Secrets are "
        "never returned — `prefix` identifies a token in this list."
    ),
    responses={200: {"description": "A page of your tokens."}, 401: UNAUTHORIZED},
)
async def list_tokens(
    service: ServiceDep, page: PageParamsDep
) -> PaginatedResponse[PersonalTokenResponse]:
    result = await service.list_tokens(page)
    return PaginatedResponse.of(
        items=[_token(token) for token in result.items],
        page=result.page,
        page_size=result.page_size,
        total_items=result.total,
    )


@router.get(
    "/{token_id}",
    response_model=ApiResponse[PersonalTokenResponse],
    summary="Get a personal access token",
    description="Returns one of your tokens. The secret is not included and cannot be re-read.",
    responses={200: {"description": "The token."}, 401: UNAUTHORIZED, 404: NOT_FOUND},
)
async def get_token(
    token_id: TokenIdPath, service: ServiceDep
) -> ApiResponse[PersonalTokenResponse]:
    return ApiResponse.ok(_token(await service.get(token_id)))


@router.post(
    "/{token_id}/revoke",
    response_model=ApiResponse[PersonalTokenResponse],
    summary="Revoke a personal access token",
    description=(
        "Kills the token. The coding agent using it is refused from its **next request** — nothing "
        "caches the decision.\n\n"
        "The row is kept rather than deleted, so the token stays visible in your list as revoked. "
        "Revoking twice is a no-op."
    ),
    responses={200: {"description": "The token is revoked."}, 401: UNAUTHORIZED, 404: NOT_FOUND},
)
async def revoke_token(
    token_id: TokenIdPath, service: ServiceDep
) -> ApiResponse[PersonalTokenResponse]:
    return ApiResponse.ok(
        _token(await service.revoke(token_id)), message="Personal access token revoked."
    )
