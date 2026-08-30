"""Reading a tenant's plan and usage (spec §5.9).

Two reads and no writes, which is the whole surface v1.1 needs. **There is deliberately no endpoint
to change a plan.** Nothing here takes payment — the pricing model and the payment provider are §9's
open question — and an endpoint that let a tenant move themselves onto the pro plan would be a
self-serve upgrade with the paying part missing. Plan changes are made out of band on the tenant
row until there is something to charge.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query

from src.modules.billing.domain.models import UsageMetric
from src.modules.billing.domain.services import BillingService, LimitUsage, PlanSnapshot
from src.modules.billing.presentation.dtos.billing import (
    LimitResponse,
    PlanResponse,
    UsageHistoryResponse,
    UsagePeriodResponse,
)
from src.modules.tenants.presentation.dependencies import CurrentTenantDep
from src.shared.database.dependencies import SessionDep
from src.shared.responses import ApiResponse, create_router

router = create_router(prefix="/billing", tags=["billing"])


def get_billing_service(session: SessionDep, tenant_id: CurrentTenantDep) -> BillingService:
    return BillingService(session, tenant_id)


ServiceDep = Annotated[BillingService, Depends(get_billing_service)]

UNAUTHORIZED = {
    "description": "Access token is missing, invalid, or revoked (`UNAUTHORIZED`, `INVALID_TOKEN`)."
}


def _limit(usage: LimitUsage) -> LimitResponse:
    return LimitResponse(
        limit=usage.limit,
        used=usage.used,
        remaining=usage.remaining,
        exceeded=usage.exceeded,
    )


def _plan(snapshot: PlanSnapshot) -> PlanResponse:
    return PlanResponse(
        plan=snapshot.plan.name,
        period=snapshot.period,
        enforced=snapshot.enforced,
        agents=_limit(snapshot.agents),
        messages=_limit(snapshot.messages),
        storage=_limit(snapshot.storage),
        prompt_tokens=snapshot.prompt_tokens,
        completion_tokens=snapshot.completion_tokens,
        cost_micro_usd=snapshot.cost_micro_usd,
    )


@router.get(
    "/plan",
    response_model=ApiResponse[PlanResponse],
    summary="Read your plan and this period's usage",
    description=(
        "What your plan allows, what you have used against it, and what is left — in one "
        "response, so a client can show every limit without asking three times.\n\n"
        "**Read this before you hit a 402.** `agents` and `storage` are current state: deleting an "
        "agent frees a slot and deleting a source frees its bytes. `messages` accumulates over the "
        "billing period and resets at the start of each month.\n\n"
        "A `limit` of `-1` means unlimited, and `remaining` is then absent rather than a large "
        "number. When `enforced` is false the figures are still counted but nothing is blocked."
    ),
    responses={200: {"description": "The plan and its usage."}, 401: UNAUTHORIZED},
)
async def read_plan(service: ServiceDep) -> ApiResponse[PlanResponse]:
    return ApiResponse.ok(_plan(await service.snapshot()))


@router.get(
    "/usage",
    response_model=ApiResponse[UsageHistoryResponse],
    summary="Read usage by billing period",
    description=(
        "Metered usage per month, newest first: messages, tokens, and estimated provider spend.\n\n"
        "These are **counters**, not a query over your conversations — they only ever go up, so a "
        "closed period reports the same figures however the underlying messages are later edited "
        "or deleted. That is what makes an invoice reproducible.\n\n"
        "Periods with no usage are absent rather than returned as zeroes."
    ),
    responses={200: {"description": "A page of billing periods."}, 401: UNAUTHORIZED},
)
async def read_usage(
    service: ServiceDep,
    periods: Annotated[
        int | None,
        Query(ge=1, le=36, description="How many recent periods to return. Defaults to 12."),
    ] = None,
) -> ApiResponse[UsageHistoryResponse]:
    history = await service.history(periods)
    return ApiResponse.ok(
        UsageHistoryResponse(
            periods=[
                UsagePeriodResponse(
                    period=period,
                    messages=counters.get(UsageMetric.MESSAGES, 0),
                    prompt_tokens=counters.get(UsageMetric.PROMPT_TOKENS, 0),
                    completion_tokens=counters.get(UsageMetric.COMPLETION_TOKENS, 0),
                    cost_micro_usd=counters.get(UsageMetric.COST_MICRO_USD, 0),
                )
                for period, counters in sorted(history.items(), reverse=True)
            ]
        )
    )
