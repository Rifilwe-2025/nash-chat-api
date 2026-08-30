"""The platform administration surface (plan Phase 15).

Small on purpose. These routes cover what has no tenant context — the account list, one account's
size and people, the platform totals, and the enable/disable lever. **Everything else an admin does,
they do through the ordinary endpoints by sending `X-Tenant-Id`**, which scopes the request to that
tenant; see `tenants/presentation/dependencies.py`. That is what gives an admin full CRUD without a
second copy of every module's API.

Every route here carries the platform-admin dependency. A signed-in tenant user gets `403`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Path, Query

from src.modules.admin.domain.services import TenantCounts, TenantDetail
from src.modules.admin.presentation.dependencies import AdminServiceDep
from src.modules.admin.presentation.dtos.admin import (
    AdminUserResponse,
    PlatformOverviewResponse,
    SetTenantStatusRequest,
    TenantCountsResponse,
    TenantDetailResponse,
    TenantSummaryResponse,
)
from src.modules.tenants.domain.models import Tenant, TenantStatus
from src.shared.database.pagination import PageParamsDep
from src.shared.responses import ApiResponse, PaginatedResponse, create_router

router = create_router(prefix="/admin", tags=["admin"])

TenantIdPath = Annotated[uuid.UUID, Path(description="Identifier of the account.")]

UNAUTHORIZED = {
    "description": "Access token is missing, invalid, or revoked (`UNAUTHORIZED`, `INVALID_TOKEN`)."
}
NOT_ADMIN = {
    "description": "The caller is signed in but is not platform staff (`PLATFORM_ADMIN_REQUIRED`)."
}
NO_TENANT = {"description": "No account with that id (`TENANT_NOT_FOUND`)."}


def _counts(counts: TenantCounts) -> TenantCountsResponse:
    return TenantCountsResponse(
        users=counts.users,
        agents=counts.agents,
        conversations=counts.conversations,
        messages=counts.messages,
        stored_bytes=counts.stored_bytes,
    )


def _summary(tenant: Tenant, counts: TenantCounts) -> TenantSummaryResponse:
    return TenantSummaryResponse(
        id=tenant.id,
        name=tenant.name,
        plan=tenant.plan,
        status=tenant.status,
        status_note=tenant.status_note,
        status_changed_at=tenant.status_changed_at,
        counts=_counts(counts),
        created_at=tenant.created_at,
    )


def _detail(detail: TenantDetail) -> TenantDetailResponse:
    tenant = detail.tenant
    return TenantDetailResponse(
        id=tenant.id,
        name=tenant.name,
        plan=tenant.plan,
        status=tenant.status,
        status_note=tenant.status_note,
        status_changed_at=tenant.status_changed_at,
        counts=_counts(detail.counts),
        created_at=tenant.created_at,
        users=[
            AdminUserResponse(
                id=user.id,
                email=user.email,
                full_name=user.full_name,
                role=user.role,
                is_platform_admin=user.is_platform_admin,
                created_at=user.created_at,
            )
            for user in detail.users
        ],
    )


@router.get(
    "/tenants",
    response_model=PaginatedResponse[TenantSummaryResponse],
    summary="List accounts",
    description=(
        "Every account on the platform, newest first, with how big each one is and whether it is "
        "enabled.\n\n"
        "Filter with `search` on the account name, or `status` to see only what is disabled. To "
        "look up the account behind a person, use `/admin/accounts/by-email` instead — an operator "
        "usually starts from the address somebody wrote in from."
    ),
    responses={
        200: {"description": "A page of accounts."},
        401: UNAUTHORIZED,
        403: NOT_ADMIN,
    },
)
async def list_tenants(
    service: AdminServiceDep,
    page: PageParamsDep,
    search: Annotated[
        str | None, Query(description="Case-insensitive substring of the account name.")
    ] = None,
    status: Annotated[
        TenantStatus | None, Query(description="Only accounts in this state.")
    ] = None,
) -> PaginatedResponse[TenantSummaryResponse]:
    result, counts = await service.list_tenants(page, search=search, status=status)
    return PaginatedResponse.of(
        items=[_summary(tenant, counts[tenant.id]) for tenant in result.items],
        page=result.page,
        page_size=result.page_size,
        total_items=result.total,
    )


@router.get(
    "/tenants/{tenant_id}",
    response_model=ApiResponse[TenantDetailResponse],
    summary="Read one account",
    description=(
        "The account, its size, and the people in it.\n\n"
        "**This does not show what is inside the account** — no agent configuration, no knowledge, "
        "no transcripts. To see those, act as the tenant: send `X-Tenant-Id: <id>` on the ordinary "
        "endpoints and they answer as though you were signed in to that account."
    ),
    responses={
        200: {"description": "The account."},
        401: UNAUTHORIZED,
        403: NOT_ADMIN,
        404: NO_TENANT,
    },
)
async def read_tenant(
    tenant_id: TenantIdPath, service: AdminServiceDep
) -> ApiResponse[TenantDetailResponse]:
    return ApiResponse.ok(_detail(await service.tenant_detail(tenant_id)))


@router.get(
    "/accounts/by-email",
    response_model=ApiResponse[TenantDetailResponse],
    summary="Find the account behind an email address",
    description=(
        "Somebody writes in and the only thing identifying them is the address they signed up "
        "with. This resolves that address to the account it belongs to, in the same shape as "
        "reading the account directly."
    ),
    responses={
        200: {"description": "The account that address belongs to."},
        401: UNAUTHORIZED,
        403: NOT_ADMIN,
        404: {"description": "No account uses that address (`USER_NOT_FOUND`)."},
    },
)
async def find_by_email(
    service: AdminServiceDep,
    email: Annotated[str, Query(description="The address to look up.", examples=["ada@acme.test"])],
) -> ApiResponse[TenantDetailResponse]:
    return ApiResponse.ok(_detail(await service.find_account_by_email(email)))


@router.put(
    "/tenants/{tenant_id}/status",
    response_model=ApiResponse[TenantSummaryResponse],
    summary="Enable or disable an account",
    description=(
        "The lever the platform has over an account.\n\n"
        "**Disabling takes effect immediately and everywhere.** Nobody in the account can sign in, "
        "existing access tokens stop working on their next request, the account's API keys are "
        "refused, and its agents answer on no channel — including WhatsApp, where inbound messages "
        "are left unanswered rather than replied to.\n\n"
        "**Nothing is deleted.** Agents, knowledge and transcripts stay exactly as they are, and "
        "re-enabling restores service with no further steps. That reversibility is the point: it "
        "is the action to take when something needs to stop *now* and be understood afterwards.\n\n"
        "The `note` is for whoever finds the account disabled later. It is never shown to the "
        "account holder, who is told only that the account is disabled and who to contact."
    ),
    responses={
        200: {"description": "The updated account."},
        401: UNAUTHORIZED,
        403: NOT_ADMIN,
        404: NO_TENANT,
    },
)
async def set_tenant_status(
    tenant_id: TenantIdPath, payload: SetTenantStatusRequest, service: AdminServiceDep
) -> ApiResponse[TenantSummaryResponse]:
    summary = await service.set_enabled(tenant_id, payload.enabled, payload.note)
    return ApiResponse.ok(
        _summary(summary.tenant, summary.counts),
        message="Account enabled." if payload.enabled else "Account disabled.",
    )


@router.delete(
    "/tenants/{tenant_id}",
    response_model=ApiResponse[None],
    summary="Delete an account permanently",
    description=(
        "Removes the account and everything in it — users, agents, knowledge, conversations, keys "
        "and channel connections — by cascade. **Irreversible.**\n\n"
        "You must pass `confirm` with the account's exact name. That friction is deliberate: every "
        "other destructive route in this API removes one object somebody chose, while this one "
        "removes an entire customer's history, and the id in the URL you are looking at is an easy "
        "thing to paste twice.\n\n"
        "Disabling is reversible and is almost always what is wanted instead."
    ),
    responses={
        200: {"description": "The account and everything in it were deleted."},
        401: UNAUTHORIZED,
        403: NOT_ADMIN,
        404: NO_TENANT,
        409: {
            "description": (
                "`confirm` does not match the account's name (`TENANT_CONFIRMATION_MISMATCH`)."
            )
        },
    },
)
async def delete_tenant(
    tenant_id: TenantIdPath,
    service: AdminServiceDep,
    confirm: Annotated[
        str,
        Query(
            description="The account's exact name, typed back as confirmation.",
            examples=["Acme Paints"],
        ),
    ],
) -> ApiResponse[None]:
    await service.delete_tenant(tenant_id, confirm)
    return ApiResponse.ok(message="Account deleted.")


@router.get(
    "/overview",
    response_model=ApiResponse[PlatformOverviewResponse],
    summary="Read the platform totals",
    description=(
        "How many accounts exist, how many are disabled, and how many users, agents and "
        "conversations there are across all of them. Counted live rather than cached, so it is a "
        "number for a person to read and not one to poll."
    ),
    responses={200: {"description": "The totals."}, 401: UNAUTHORIZED, 403: NOT_ADMIN},
)
async def read_overview(service: AdminServiceDep) -> ApiResponse[PlatformOverviewResponse]:
    totals = await service.overview()
    return ApiResponse.ok(
        PlatformOverviewResponse(
            tenants=totals.tenants,
            active_tenants=totals.active_tenants,
            disabled_tenants=totals.disabled_tenants,
            users=totals.users,
            agents=totals.agents,
            conversations=totals.conversations,
        )
    )
