"""Reading what the platform recorded (spec §5.8).

Every route here is a read. Analytics owns no behaviour a tenant can change, which is why there is
no POST, PATCH or DELETE in this file — the numbers are produced by the modules that do the work,
and this is the surface that reports them.

Authenticated with the caller's access token, so the tenant is resolved from the token and every
query below is scoped before it is written. The one exception is ``/analytics/operations``, which
reports process telemetry for whoever operates the deployment and is gated on the operator secret
instead — see ``presentation/dependencies.py`` for why a tenant token is the wrong credential there.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, Path, Query

from src.modules.analytics.domain.services import (
    AnalyticsService,
    FailureReport,
    TraceEntry,
    UsageReport,
)
from src.modules.analytics.internal import windows
from src.modules.analytics.presentation.dependencies import OperatorDep
from src.modules.analytics.presentation.dtos.analytics import (
    ChannelPointResponse,
    ConversationCountsResponse,
    DailyPointResponse,
    FailureClassResponse,
    FailureItemResponse,
    FailureReportResponse,
    MessageTotalsResponse,
    ModelPointResponse,
    OperationsResponse,
    QualityResponse,
    TraceEntryResponse,
    UsageReportResponse,
    WindowResponse,
)
from src.modules.tenants.presentation.dependencies import CurrentTenantDep
from src.shared.database.dependencies import SessionDep
from src.shared.database.pagination import PageParamsDep
from src.shared.observability import metrics
from src.shared.responses import ApiResponse, PaginatedResponse, create_router

router = create_router(tags=["analytics"])


def get_analytics_service(session: SessionDep, tenant_id: CurrentTenantDep) -> AnalyticsService:
    return AnalyticsService(session, tenant_id)


ServiceDep = Annotated[AnalyticsService, Depends(get_analytics_service)]

FromQuery = Annotated[
    datetime | None,
    Query(
        alias="from",
        description=(
            "Start of the reporting window, inclusive, as an ISO-8601 timestamp. A value with no "
            "timezone is read as UTC. Defaults to 30 days before `to`."
        ),
        examples=["2026-08-01T00:00:00Z"],
    ),
]
ToQuery = Annotated[
    datetime | None,
    Query(
        alias="to",
        description="End of the window, exclusive. Defaults to now.",
        examples=["2026-09-01T00:00:00Z"],
    ),
]
PreviewQuery = Annotated[
    bool,
    Query(
        alias="includePreview",
        description=(
            "Count conversations from the builder's test chat as well as real traffic. Off by "
            "default — previews are you trying your own agent, not customers using it."
        ),
    ),
]

UNAUTHORIZED = {
    "description": "Access token is missing, invalid, or revoked (`UNAUTHORIZED`, `INVALID_TOKEN`)."
}
BAD_WINDOW = {
    "description": (
        "`from` is not before `to` (`ANALYTICS_WINDOW_INVALID`), or the window is longer than the "
        "configured maximum (`ANALYTICS_WINDOW_TOO_LONG`)."
    )
}


def _window(window: windows.Window) -> WindowResponse:
    return WindowResponse(start=window.start, end=window.end, days=window.days)


def _report(report: UsageReport) -> UsageReportResponse:
    messages = report.messages
    return UsageReportResponse(
        window=_window(report.window),
        agent_id=report.agent_id,
        includes_preview=report.include_preview,
        conversations=ConversationCountsResponse(
            started=report.conversations.started,
            escalated=report.conversations.escalated,
            open_now=report.conversations.open_now,
        ),
        messages=MessageTotalsResponse(
            total=messages.total,
            user=messages.user,
            assistant=messages.assistant,
            prompt_tokens=messages.prompt_tokens,
            completion_tokens=messages.completion_tokens,
            total_tokens=messages.prompt_tokens + messages.completion_tokens,
            cost_micro_usd=messages.cost_micro_usd,
            priced_messages=messages.priced_messages,
        ),
        quality=QualityResponse(
            answered=report.quality.answered,
            without_context=report.quality.without_context,
            declined=report.quality.declined,
            fallback_rate=report.quality.fallback_rate,
            conversations=report.quality.conversations,
            escalated=report.quality.escalated,
            escalation_rate=report.quality.escalation_rate,
        ),
        daily=[
            DailyPointResponse(
                day=point.day,
                messages=point.messages,
                conversations=point.conversations,
                tokens=point.tokens,
                cost_micro_usd=point.cost_micro_usd,
            )
            for point in report.daily
        ],
        channels=[
            ChannelPointResponse(
                channel=point.channel, conversations=point.conversations, messages=point.messages
            )
            for point in report.channels
        ],
        models=[
            ModelPointResponse(
                provider=point.provider,
                model=point.model,
                messages=point.messages,
                prompt_tokens=point.prompt_tokens,
                completion_tokens=point.completion_tokens,
                cost_micro_usd=point.cost_micro_usd,
            )
            for point in report.models
        ],
    )


def _failures(report: FailureReport) -> FailureReportResponse:
    return FailureReportResponse(
        window=_window(report.window),
        total=report.total,
        classes=[
            FailureClassResponse(
                kind=failure_class.kind,
                count=failure_class.count,
                recent=[
                    FailureItemResponse(
                        occurred_at=item.occurred_at,
                        kind=item.kind,
                        code=item.code,
                        detail=item.detail,
                        subject=item.subject,
                        subject_id=item.subject_id,
                        agent_id=item.agent_id,
                    )
                    for item in failure_class.recent
                ],
            )
            for failure_class in report.classes
        ],
    )


def _trace(entry: TraceEntry) -> TraceEntryResponse:
    message = entry.message
    return TraceEntryResponse(
        id=message.id,
        sequence=message.sequence,
        role=message.role,
        content=message.content,
        created_at=message.created_at,
        provider=message.provider,
        model=message.model,
        prompt_tokens=message.prompt_tokens,
        completion_tokens=message.completion_tokens,
        cost_micro_usd=message.cost_micro_usd,
        citations=[dict(citation) for citation in entry.citations if isinstance(citation, dict)],
        tier=entry.tier,
        has_context=entry.has_context,
        tool_calls=[call for call in entry.tool_calls if isinstance(call, dict)],
        guardrail=entry.guardrail,
    )


# -- usage ---------------------------------------------------------------------------


@router.get(
    "/analytics/usage",
    response_model=ApiResponse[UsageReportResponse],
    summary="Read usage, cost and quality for the whole tenant",
    description=(
        "Everything a usage dashboard needs for one period, in one response: conversation and "
        "message counts, tokens, estimated cost split by model, a per-day series, and the two "
        "quality signals.\n\n"
        "**The figures reconcile with the transcript.** Tokens are what the provider reported when "
        "each message was written, and cost was priced at the same moment — nothing here is "
        "re-estimated later, so a total is the sum of the rows you can go and read.\n\n"
        "**Cost may be a floor.** A model with no configured price records its tokens but no cost; "
        "compare `pricedMessages` against `assistant` to see whether that happened."
    ),
    responses={
        200: {"description": "The usage report."},
        401: UNAUTHORIZED,
        422: BAD_WINDOW,
    },
)
async def read_usage(
    service: ServiceDep,
    start: FromQuery = None,
    end: ToQuery = None,
    include_preview: PreviewQuery = False,
) -> ApiResponse[UsageReportResponse]:
    report = await service.report(windows.resolve(start, end), include_preview=include_preview)
    return ApiResponse.ok(_report(report))


@router.get(
    "/agents/{agent_id}/analytics",
    response_model=ApiResponse[UsageReportResponse],
    summary="Read one agent's usage dashboard",
    description=(
        "The same payload as `/analytics/usage`, narrowed to a single agent — the per-agent "
        "dashboard of §5.8.\n\n"
        "Read `quality.fallbackRate` alongside `quality.withoutContext`: a high rate means the "
        "agent is regularly being asked things its knowledge base cannot answer, which is a "
        "knowledge problem rather than a model one. A high `escalationRate` means your escalation "
        "triggers are firing often — sometimes correct, sometimes a trigger phrased too broadly."
    ),
    responses={
        200: {"description": "The agent's usage report."},
        401: UNAUTHORIZED,
        404: {"description": "No such agent in your tenant (`AGENT_NOT_FOUND`)."},
        422: BAD_WINDOW,
    },
)
async def read_agent_usage(
    agent_id: Annotated[uuid.UUID, Path(description="Identifier of the agent.")],
    service: ServiceDep,
    start: FromQuery = None,
    end: ToQuery = None,
    include_preview: PreviewQuery = False,
) -> ApiResponse[UsageReportResponse]:
    report = await service.report(
        windows.resolve(start, end), agent_id=agent_id, include_preview=include_preview
    )
    return ApiResponse.ok(_report(report))


# -- conversation trace --------------------------------------------------------------


@router.get(
    "/analytics/conversations/{conversation_id}/trace",
    response_model=PaginatedResponse[TraceEntryResponse],
    summary="Read a conversation with its citation trace",
    description=(
        "The debugging view: every message in order, and for each reply the sources it was "
        "grounded in, which retrieval tier ran, whether anything relevant was found, which tools "
        "were called, and what the turn cost.\n\n"
        "**This is why the agent said that.** `citations` is what was actually put in front of the "
        "model at the time, not what would be retrieved for the same question today — a trace that "
        "re-ran retrieval would answer a different question. `hasContext: false` on a reply is the "
        "agent's \"I don't know\" case; `guardrail` is set when the answer came from a rule rather "
        "than from the model at all."
    ),
    responses={
        200: {"description": "A page of the transcript, oldest first."},
        401: UNAUTHORIZED,
        404: {"description": "No such conversation in your tenant (`CONVERSATION_NOT_FOUND`)."},
    },
)
async def read_trace(
    conversation_id: Annotated[uuid.UUID, Path(description="Identifier of the conversation.")],
    service: ServiceDep,
    page: PageParamsDep,
) -> PaginatedResponse[TraceEntryResponse]:
    result = await service.trace(conversation_id, page)
    return PaginatedResponse.of(
        items=[_trace(entry) for entry in result.items],
        page=result.page,
        page_size=result.page_size,
        total_items=result.total,
    )


# -- failures ------------------------------------------------------------------------


@router.get(
    "/analytics/failures",
    response_model=ApiResponse[FailureReportResponse],
    summary="Read everything that failed",
    description=(
        "One report over the five failure classes of §5.8, each read from wherever it is actually "
        "recorded:\n\n"
        "- **ingestion** — sources whose extraction failed, with the reason.\n"
        "- **provider** — model calls that failed with nobody waiting on them (a WhatsApp reply "
        "that never got written). A chat call that fails answers its caller with a 409 and a "
        "request id instead, so it is not repeated here.\n"
        "- **webhook** — your endpoints that are failing **right now**, by consecutive failure "
        "count. Unlike the others this is current state rather than events in the window.\n"
        "- **channel** — WhatsApp messages that were never delivered.\n"
        "- **tool** — tool calls that failed, timed out, or were refused by the allowlist.\n\n"
        "Each class carries a count for the window and the most recent few items. To go deeper, "
        "the owning endpoint has the full list: sources, tool call logs, and webhook endpoints."
    ),
    responses={
        200: {"description": "The failure report."},
        401: UNAUTHORIZED,
        422: BAD_WINDOW,
    },
)
async def read_failures(
    service: ServiceDep,
    start: FromQuery = None,
    end: ToQuery = None,
    recent_limit: Annotated[
        int,
        Query(
            alias="recentLimit",
            ge=1,
            le=50,
            description="How many recent items to include per class.",
        ),
    ] = 5,
) -> ApiResponse[FailureReportResponse]:
    report = await service.failures(windows.resolve(start, end), recent_limit)
    return ApiResponse.ok(_failures(report))


# -- operations ----------------------------------------------------------------------


@router.get(
    "/analytics/operations",
    response_model=ApiResponse[OperationsResponse],
    summary="Read this process's operational metrics",
    description=(
        "Request counts and latency by route, provider call counts, latency and errors — for "
        "whoever operates the deployment, not for a tenant. Authenticated with the operator secret "
        "in `X-Operator-Token` rather than an access token, because these numbers span every "
        "tenant.\n\n"
        "**In-memory and per process.** Counters start at zero when the process starts, and a "
        "deployment running four workers has four independent views. They are telemetry, not a "
        "record: anything a tenant may need to look up later is stored in a table instead.\n\n"
        "Disabled unless `OBSERVABILITY_OPERATOR_TOKEN` is configured — a deployment that has not "
        "set one gets a 503 rather than an open endpoint."
    ),
    responses={
        200: {"description": "A snapshot of this process's counters and timings."},
        403: {"description": "The operator token is missing or wrong (`OPERATOR_TOKEN_INVALID`)."},
        503: {"description": "No operator token is configured (`METRICS_DISABLED`)."},
    },
)
async def read_operations(_: OperatorDep) -> ApiResponse[OperationsResponse]:
    snapshot: dict[str, Any] = metrics.snapshot()
    return ApiResponse.ok(
        OperationsResponse(
            uptime_seconds=snapshot["uptimeSeconds"],
            counters=snapshot["counters"],
            timings=snapshot["timings"],
            series_dropped=snapshot["seriesDropped"],
        )
    )
