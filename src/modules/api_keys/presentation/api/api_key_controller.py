from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Path, Query

from src.modules.api_keys.domain.models import ApiKey
from src.modules.api_keys.domain.services import ApiKeyService
from src.modules.api_keys.internal.key_generator import GeneratedKey
from src.modules.api_keys.presentation.dtos.api_key import (
    ApiKeyResponse,
    IssueApiKeyRequest,
    IssuedApiKeyResponse,
    UpdateApiKeyRequest,
)
from src.modules.tenants.presentation.dependencies import CurrentTenantDep
from src.shared.database.dependencies import SessionDep
from src.shared.database.pagination import PageParamsDep
from src.shared.responses import ApiResponse, PaginatedResponse, create_router

router = create_router(prefix="/api-keys", tags=["api-keys"])


def get_api_key_service(session: SessionDep, tenant_id: CurrentTenantDep) -> ApiKeyService:
    """The tenant comes from the token, so every query below is scoped before it is written."""
    return ApiKeyService(session, tenant_id)


ServiceDep = Annotated[ApiKeyService, Depends(get_api_key_service)]
KeyIdPath = Annotated[uuid.UUID, Path(description="Identifier of the API key.")]

UNAUTHORIZED = {
    "description": "Access token is missing, invalid, or revoked (`UNAUTHORIZED`, `INVALID_TOKEN`)."
}
NOT_FOUND = {
    "description": (
        "No such API key in your tenant (`API_KEY_NOT_FOUND`). Another tenant's key is reported as "
        "missing rather than forbidden."
    )
}


def _api_key(api_key: ApiKey) -> ApiKeyResponse:
    return ApiKeyResponse(
        id=api_key.id,
        agent_id=api_key.agent_id,
        name=api_key.name,
        prefix=api_key.prefix,
        scopes=[str(scope) for scope in api_key.scopes],
        rate_limit_per_minute=api_key.rate_limit_per_minute,
        last_used_at=api_key.last_used_at,
        revoked_at=api_key.revoked_at,
        expires_at=api_key.expires_at,
        active=api_key.is_active,
        created_at=api_key.created_at,
    )


@router.post(
    "",
    response_model=ApiResponse[IssuedApiKeyResponse],
    status_code=201,
    summary="Issue an API key",
    description=(
        "Creates a key for one of your agents and **returns the secret once**. It is stored only "
        "as a hash, so this response is the only time it exists anywhere — copy it now. If it is "
        "lost, issue a new key and revoke this one; nobody, including us, can recover it.\n\n"
        "Give a key only the scopes it needs. A website widget that sends messages does not need "
        "`chat:read`, and a reporting job should never hold `chat:write`.\n\n"
        "The key works as soon as the agent is published, and stops the moment it is revoked."
    ),
    responses={
        201: {"description": "The key was issued. The secret is in `value.key`."},
        401: UNAUTHORIZED,
        404: {"description": "No such agent in your tenant (`AGENT_NOT_FOUND`)."},
        422: {
            "description": (
                "An unknown scope (`UNKNOWN_SCOPE`), no scopes at all (`API_KEY_NEEDS_SCOPE`), a "
                "rate limit outside the permitted range (`INVALID_RATE_LIMIT`), or an expiry in "
                "the past (`API_KEY_EXPIRY_IN_PAST`)."
            )
        },
    },
)
async def issue_key(
    payload: IssueApiKeyRequest,
    service: ServiceDep,
    agent_id: Annotated[
        uuid.UUID, Query(alias="agentId", description="Agent this key speaks for.")
    ],
) -> ApiResponse[IssuedApiKeyResponse]:
    api_key, generated = await service.issue(
        agent_id=agent_id,
        name=payload.name,
        scopes=[scope.value for scope in payload.scopes] if payload.scopes else None,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        expires_at=payload.expires_at,
    )
    return ApiResponse.ok(
        _issued(api_key, generated),
        message="Store this key now — it will not be shown again.",
    )


def _issued(api_key: ApiKey, generated: GeneratedKey) -> IssuedApiKeyResponse:
    return IssuedApiKeyResponse(key=generated.secret, api_key=_api_key(api_key))


@router.get(
    "",
    response_model=PaginatedResponse[ApiKeyResponse],
    summary="List API keys",
    description=(
        "Lists your keys, newest first, with their scopes, limits and last use. Secrets are never "
        "returned — `prefix` is what identifies a key in this list. Filter by `agentId` to see one "
        "agent's keys."
    ),
    responses={
        200: {"description": "A page of your keys."},
        401: UNAUTHORIZED,
        404: {"description": "No such agent in your tenant (`AGENT_NOT_FOUND`)."},
    },
)
async def list_keys(
    service: ServiceDep,
    page: PageParamsDep,
    agent_id: Annotated[
        uuid.UUID | None, Query(alias="agentId", description="Only this agent's keys.")
    ] = None,
) -> PaginatedResponse[ApiKeyResponse]:
    result = await service.list_keys(page, agent_id=agent_id)
    return PaginatedResponse.of(
        items=[_api_key(api_key) for api_key in result.items],
        page=result.page,
        page_size=result.page_size,
        total_items=result.total,
    )


@router.get(
    "/{key_id}",
    response_model=ApiResponse[ApiKeyResponse],
    summary="Get an API key",
    description=(
        "Returns one key's configuration. The secret is not included and cannot be re-read."
    ),
    responses={200: {"description": "The key."}, 401: UNAUTHORIZED, 404: NOT_FOUND},
)
async def get_key(key_id: KeyIdPath, service: ServiceDep) -> ApiResponse[ApiKeyResponse]:
    return ApiResponse.ok(_api_key(await service.get(key_id)))


@router.patch(
    "/{key_id}",
    response_model=ApiResponse[ApiKeyResponse],
    summary="Update an API key",
    description=(
        "Changes a key's name, scopes, or rate limit without reissuing it — the secret in your "
        "integration keeps working. Narrowing scopes takes effect on the next request."
    ),
    responses={
        200: {"description": "The updated key."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
        422: {
            "description": (
                "An unknown scope (`UNKNOWN_SCOPE`) or an out-of-range limit "
                "(`INVALID_RATE_LIMIT`)."
            )
        },
    },
)
async def update_key(
    key_id: KeyIdPath, payload: UpdateApiKeyRequest, service: ServiceDep
) -> ApiResponse[ApiKeyResponse]:
    api_key = await service.update(
        key_id,
        name=payload.name,
        scopes=[scope.value for scope in payload.scopes] if payload.scopes else None,
        rate_limit_per_minute=payload.rate_limit_per_minute,
    )
    return ApiResponse.ok(_api_key(api_key))


@router.post(
    "/{key_id}/revoke",
    response_model=ApiResponse[ApiKeyResponse],
    summary="Revoke an API key",
    description=(
        "Kills the key. It is refused from the **next request onward** — nothing caches the "
        "decision, so there is no window in which a revoked key still works.\n\n"
        "The row is kept rather than deleted so the key stays visible in your list as revoked, and "
        "so its past use remains attributable. Revoking twice is a no-op."
    ),
    responses={
        200: {"description": "The key is revoked."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
    },
)
async def revoke_key(key_id: KeyIdPath, service: ServiceDep) -> ApiResponse[ApiKeyResponse]:
    return ApiResponse.ok(_api_key(await service.revoke(key_id)), message="API key revoked.")
