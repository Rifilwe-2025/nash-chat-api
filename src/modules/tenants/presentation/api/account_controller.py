from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from src.modules.tenants.domain.models import Tenant, User
from src.modules.tenants.domain.services import TenantService
from src.modules.tenants.presentation.dependencies import CurrentTenantDep, CurrentUserDep
from src.modules.tenants.presentation.dtos.account import (
    MeResponse,
    TenantResponse,
    UpdateProfileRequest,
    UpdateTenantRequest,
    UserResponse,
)
from src.shared.database.dependencies import SessionDep
from src.shared.responses import ApiResponse, create_router

router = create_router(tags=["account"])


def get_tenant_service(session: SessionDep) -> TenantService:
    return TenantService(session)


TenantServiceDep = Annotated[TenantService, Depends(get_tenant_service)]

UNAUTHORIZED_RESPONSE = {
    "description": "Access token is missing, invalid, or revoked (`UNAUTHORIZED`, `INVALID_TOKEN`)."
}


def _user(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role.value,
        tenant_id=user.tenant_id,
        is_platform_admin=user.is_platform_admin,
    )


def _tenant(tenant: Tenant) -> TenantResponse:
    return TenantResponse(
        id=tenant.id, name=tenant.name, plan=tenant.plan.value, status=tenant.status.value
    )


@router.get(
    "/me",
    response_model=ApiResponse[MeResponse],
    summary="Get the signed-in account",
    description=(
        "Returns the authenticated user together with the tenant they belong to. The tenant is "
        "resolved from the access token, never from a parameter."
    ),
    responses={200: {"description": "The caller's account."}, 401: UNAUTHORIZED_RESPONSE},
)
async def me(
    user: CurrentUserDep, tenant_id: CurrentTenantDep, service: TenantServiceDep
) -> ApiResponse[MeResponse]:
    tenant = await service.get_tenant(tenant_id)
    return ApiResponse.ok(MeResponse(user=_user(user), tenant=_tenant(tenant)))


@router.patch(
    "/me",
    response_model=ApiResponse[UserResponse],
    summary="Update the signed-in profile",
    description=(
        "Updates the caller's display name and/or email. Omitted fields are left unchanged. "
        "Changing to an email that already exists is rejected."
    ),
    responses={
        200: {"description": "Profile updated."},
        401: UNAUTHORIZED_RESPONSE,
        409: {"description": "That email belongs to another account (`EMAIL_TAKEN`)."},
        422: {"description": "The payload failed validation (`VALIDATION_ERROR`)."},
    },
)
async def update_me(
    payload: UpdateProfileRequest, user: CurrentUserDep, service: TenantServiceDep
) -> ApiResponse[UserResponse]:
    updated = await service.update_profile(user, full_name=payload.full_name, email=payload.email)
    return ApiResponse.ok(_user(updated))


@router.get(
    "/tenant",
    response_model=ApiResponse[TenantResponse],
    summary="Get the current tenant",
    description="Returns the organisation the caller belongs to, resolved from the access token.",
    responses={200: {"description": "The caller's tenant."}, 401: UNAUTHORIZED_RESPONSE},
)
async def get_tenant(
    tenant_id: CurrentTenantDep, service: TenantServiceDep
) -> ApiResponse[TenantResponse]:
    return ApiResponse.ok(_tenant(await service.get_tenant(tenant_id)))


@router.patch(
    "/tenant",
    response_model=ApiResponse[TenantResponse],
    summary="Rename the current tenant",
    description="Renames the caller's own organisation. No other tenant can be addressed.",
    responses={
        200: {"description": "Tenant renamed."},
        401: UNAUTHORIZED_RESPONSE,
        422: {"description": "The payload failed validation (`VALIDATION_ERROR`)."},
    },
)
async def rename_tenant(
    payload: UpdateTenantRequest, tenant_id: CurrentTenantDep, service: TenantServiceDep
) -> ApiResponse[TenantResponse]:
    return ApiResponse.ok(_tenant(await service.rename_tenant(tenant_id, payload.name)))


@router.get(
    "/tenant/members",
    response_model=ApiResponse[list[UserResponse]],
    summary="List members of the current tenant",
    description=(
        "Lists every user belonging to the caller's tenant. v1 creates one owner per tenant; "
        "in-tenant team accounts are a later feature."
    ),
    responses={200: {"description": "Members of the caller's tenant."}, 401: UNAUTHORIZED_RESPONSE},
)
async def list_members(
    tenant_id: CurrentTenantDep, service: TenantServiceDep
) -> ApiResponse[list[UserResponse]]:
    members = await service.list_members(tenant_id)
    return ApiResponse.ok([_user(member) for member in members])
