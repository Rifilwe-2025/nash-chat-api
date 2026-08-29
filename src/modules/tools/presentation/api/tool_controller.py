"""Configuring an agent's live API tools (spec §5.2.1 Pattern A).

Authenticated by the user's access token, so everything is scoped to the caller's own tenant. There
is no public surface here — a tool is never called by a customer directly; it is called by the
model, mid-turn, through the conversation engine.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, Path

from src.modules.tenants.presentation.dependencies import CurrentTenantDep
from src.modules.tools.domain.models import AgentTool, ToolCallLog, ToolPolicy
from src.modules.tools.domain.services import ToolService
from src.modules.tools.presentation.dtos.tool import (
    CreateToolRequest,
    ToolCallResponse,
    ToolPolicyRequest,
    ToolPolicyResponse,
    ToolResponse,
    TryToolRequest,
    TryToolResponse,
    UpdateToolRequest,
)
from src.shared.database.dependencies import SessionDep
from src.shared.database.pagination import PageParamsDep
from src.shared.responses import ApiResponse, PaginatedResponse, create_router

router = create_router(tags=["tools"])


def get_tool_service(session: SessionDep, tenant_id: CurrentTenantDep) -> ToolService:
    """The tenant comes from the token, so every query below is scoped before it is written."""
    return ToolService(session, tenant_id)


ServiceDep = Annotated[ToolService, Depends(get_tool_service)]
AgentIdPath = Annotated[uuid.UUID, Path(description="Identifier of the agent.")]
ToolIdPath = Annotated[uuid.UUID, Path(description="Identifier of the tool.")]

UNAUTHORIZED = {
    "description": "Access token is missing, invalid, or revoked (`UNAUTHORIZED`, `INVALID_TOKEN`)."
}
AGENT_NOT_FOUND = {"description": "No such agent in your tenant (`AGENT_NOT_FOUND`)."}
TOOL_NOT_FOUND = {"description": "No such tool in your tenant (`TOOL_NOT_FOUND`)."}
INVALID_TOOL = {
    "description": (
        "The name is not a valid function name (`TOOL_NAME_INVALID`), the description is too "
        "short to be useful as prompt text (`TOOL_DESCRIPTION_TOO_SHORT`), the endpoint is not a "
        "usable http(s) URL (`TOOL_ENDPOINT_INVALID`), or it uses a placeholder the request "
        "schema does not declare (`TOOL_PLACEHOLDER_UNDECLARED`)."
    )
}


def _tool(tool: AgentTool) -> ToolResponse:
    return ToolResponse(
        id=tool.id,
        agent_id=tool.agent_id,
        name=tool.name,
        description=tool.description,
        endpoint_url=tool.endpoint_url,
        http_method=tool.http_method,
        auth_type=tool.auth_type,
        # The credential itself never leaves the server — only whether one is set.
        has_credential=bool(tool.auth_config_json),
        request_schema=tool.request_schema_json,
        response_mapping=tool.response_mapping_json,
        status=tool.status,
        timeout_seconds=tool.timeout_seconds,
        cache_ttl_seconds=tool.cache_ttl_seconds,
        last_called_at=tool.last_called_at,
        consecutive_failures=tool.consecutive_failures,
        last_error=tool.last_error,
        created_at=tool.created_at,
        updated_at=tool.updated_at,
    )


def _policy(policy: ToolPolicy) -> ToolPolicyResponse:
    return ToolPolicyResponse(
        agent_id=policy.agent_id,
        allowed_hosts=[str(host) for host in policy.allowed_hosts],
        max_calls_per_turn=policy.max_calls_per_turn,
        updated_at=policy.updated_at,
    )


def _call(call: ToolCallLog) -> ToolCallResponse:
    return ToolCallResponse(
        id=call.id,
        outcome=call.outcome,
        arguments=call.arguments_json,
        status_code=call.status_code,
        duration_ms=call.duration_ms,
        result_text=call.result_text,
        error_detail=call.error_detail,
        conversation_id=call.conversation_id,
        created_at=call.created_at,
    )


# -- tools ---------------------------------------------------------------------------


@router.post(
    "/agents/{agent_id}/tools",
    response_model=ApiResponse[ToolResponse],
    status_code=201,
    summary="Add a live API tool to an agent",
    description=(
        "Gives this agent a lookup it can make while answering — an order status, a booking, live "
        "stock. The model decides when to call it from the `description`, so write that as an "
        "instruction to the model rather than as documentation for a person.\n\n"
        "**The host must be allowed.** The first tool you add seeds the agent's allowed hosts with "
        "its own; after that, add hosts explicitly through the tool policy. A tool whose host is "
        "not allowed is refused at call time and never leaves the server.\n\n"
        "**Your credential stays here.** `authConfig` is injected into the outbound request "
        "server-side and is never shown to the model, the customer, or this API again."
    ),
    responses={
        201: {"description": "The tool was created."},
        401: UNAUTHORIZED,
        404: AGENT_NOT_FOUND,
        409: {"description": "This agent already has a tool with that name (`TOOL_NAME_TAKEN`)."},
        422: INVALID_TOOL,
    },
)
async def create_tool(
    agent_id: AgentIdPath, payload: CreateToolRequest, service: ServiceDep
) -> ApiResponse[ToolResponse]:
    tool = await service.create(
        agent_id,
        name=payload.name,
        description=payload.description,
        endpoint_url=payload.endpoint_url,
        http_method=payload.http_method,
        auth_type=payload.auth_type,
        auth_config=payload.auth_config,
        request_schema=payload.request_schema,
        response_mapping=payload.response_mapping,
        timeout_seconds=payload.timeout_seconds,
        cache_ttl_seconds=payload.cache_ttl_seconds,
    )
    return ApiResponse.ok(_tool(tool), message="Tool created.")


@router.get(
    "/agents/{agent_id}/tools",
    response_model=PaginatedResponse[ToolResponse],
    summary="List an agent's tools",
    description=(
        "Every tool defined for this agent, enabled or not, with its recent health. A rising "
        "`consecutiveFailures` with a `lastError` means your endpoint is failing and the agent is "
        "currently apologising to customers instead of answering them."
    ),
    responses={200: {"description": "A page of tools."}, 401: UNAUTHORIZED, 404: AGENT_NOT_FOUND},
)
async def list_tools(
    agent_id: AgentIdPath, service: ServiceDep, page: PageParamsDep
) -> PaginatedResponse[ToolResponse]:
    result = await service.list_tools(agent_id, page)
    return PaginatedResponse.of(
        items=[_tool(tool) for tool in result.items],
        page=result.page,
        page_size=result.page_size,
        total_items=result.total,
    )


@router.get(
    "/tools/{tool_id}",
    response_model=ApiResponse[ToolResponse],
    summary="Get a tool",
    description="One tool's configuration and health. The stored credential is never returned.",
    responses={200: {"description": "The tool."}, 401: UNAUTHORIZED, 404: TOOL_NOT_FOUND},
)
async def get_tool(tool_id: ToolIdPath, service: ServiceDep) -> ApiResponse[ToolResponse]:
    return ApiResponse.ok(_tool(await service.get(tool_id)))


@router.patch(
    "/tools/{tool_id}",
    response_model=ApiResponse[ToolResponse],
    summary="Update a tool",
    description=(
        "Changes only what you send; omitted fields are left as they were, so rotating a "
        "credential does not mean re-sending the schema.\n\n"
        "Set `status` to `disabled` to take the tool out of the prompt immediately while keeping "
        "its configuration — what you want when an integration starts misbehaving."
    ),
    responses={
        200: {"description": "The updated tool."},
        401: UNAUTHORIZED,
        404: TOOL_NOT_FOUND,
        409: {"description": "Another tool on this agent has that name (`TOOL_NAME_TAKEN`)."},
        422: INVALID_TOOL,
    },
)
async def update_tool(
    tool_id: ToolIdPath, payload: UpdateToolRequest, service: ServiceDep
) -> ApiResponse[ToolResponse]:
    changes: dict[str, Any] = payload.model_dump(exclude_unset=True, by_alias=False)
    return ApiResponse.ok(_tool(await service.update(tool_id, changes)))


@router.delete(
    "/tools/{tool_id}",
    response_model=ApiResponse[None],
    summary="Delete a tool",
    description=(
        "Removes the tool, its stored credential and its call log. The agent stops offering it on "
        "the next message. To stop it temporarily instead, set `status` to `disabled`."
    ),
    responses={
        200: {"description": "The tool was deleted."},
        401: UNAUTHORIZED,
        404: TOOL_NOT_FOUND,
    },
)
async def delete_tool(tool_id: ToolIdPath, service: ServiceDep) -> ApiResponse[None]:
    await service.delete(tool_id)
    return ApiResponse.ok(message="Tool deleted.")


@router.post(
    "/tools/{tool_id}/try",
    response_model=ApiResponse[TryToolResponse],
    status_code=200,
    summary="Run a tool with your own arguments",
    description=(
        "Executes the tool exactly as a turn would — same allowlist, same schema check, same "
        "timeout, same response mapping — and shows you **the text the model would receive**.\n\n"
        "That last part is the point. Reading what your mapping actually produces is the fastest "
        "way to find out that `fields` names a path that does not exist, or that the whole "
        "response is being sent when you meant to narrow it.\n\n"
        "A failure comes back `200` with the failure note in `resultText`, because that is what "
        "the model would get — an error here would hide the thing you are trying to see. The run "
        "is recorded in the call log with no conversation attached."
    ),
    responses={
        200: {"description": "The tool ran; `outcome` says how it went."},
        401: UNAUTHORIZED,
        404: TOOL_NOT_FOUND,
    },
)
async def try_tool(
    tool_id: ToolIdPath, payload: TryToolRequest, service: ServiceDep
) -> ApiResponse[TryToolResponse]:
    _, result = await service.try_out(tool_id, payload.arguments)
    return ApiResponse.ok(
        TryToolResponse(
            outcome=result.outcome,
            duration_ms=result.duration_ms,
            result_text=result.text,
            call_id=result.call_id,
        )
    )


@router.get(
    "/tools/{tool_id}/calls",
    response_model=PaginatedResponse[ToolCallResponse],
    summary="Read a tool's call log",
    description=(
        "Every execution, newest first: the arguments the model chose, how long it took, what "
        "came back, and what the model was shown.\n\n"
        "This is where you find out whether a bad answer was the tool's fault or the model's. "
        "`arguments` shows what the model asked for — a call made with nonsense arguments is "
        "usually a `description` that needs rewriting, not a broken endpoint."
    ),
    responses={200: {"description": "A page of calls."}, 401: UNAUTHORIZED, 404: TOOL_NOT_FOUND},
)
async def list_tool_calls(
    tool_id: ToolIdPath, service: ServiceDep, page: PageParamsDep
) -> PaginatedResponse[ToolCallResponse]:
    result = await service.call_log(tool_id, page)
    return PaginatedResponse.of(
        items=[_call(call) for call in result.items],
        page=result.page,
        page_size=result.page_size,
        total_items=result.total,
    )


# -- the allowlist -------------------------------------------------------------------


@router.get(
    "/agents/{agent_id}/tools/policy",
    response_model=ApiResponse[ToolPolicyResponse],
    summary="Get an agent's tool policy",
    description=(
        "The hosts this agent's tools may call, and how many calls one message may make. "
        "Created with the first tool you add, seeded with that tool's host."
    ),
    responses={200: {"description": "The policy."}, 401: UNAUTHORIZED, 404: AGENT_NOT_FOUND},
)
async def get_tool_policy(
    agent_id: AgentIdPath, service: ServiceDep
) -> ApiResponse[ToolPolicyResponse]:
    return ApiResponse.ok(_policy(await service.get_policy(agent_id)))


@router.put(
    "/agents/{agent_id}/tools/policy",
    response_model=ApiResponse[ToolPolicyResponse],
    summary="Set an agent's tool policy",
    description=(
        "**The allowlist is the control that matters.** Tool arguments are written by a language "
        "model from whatever a stranger typed, so this list is the boundary that holds even if "
        "everything else about a tool is misconfigured: no call can reach a host that is not on "
        "it. An empty list means no tool runs at all.\n\n"
        "A leading dot allows subdomains — `.example.com` matches `api.example.com`. Without one "
        "the match is exact, so `example.com` does not admit `example.com.attacker.test`.\n\n"
        "Hosts that resolve to private or internal addresses are refused regardless of this list."
    ),
    responses={
        200: {"description": "The updated policy."},
        401: UNAUTHORIZED,
        404: AGENT_NOT_FOUND,
        422: {"description": "`maxCallsPerTurn` is below one (`TOOL_POLICY_INVALID`)."},
    },
)
async def set_tool_policy(
    agent_id: AgentIdPath, payload: ToolPolicyRequest, service: ServiceDep
) -> ApiResponse[ToolPolicyResponse]:
    policy = await service.set_policy(
        agent_id,
        allowed_hosts=payload.allowed_hosts,
        max_calls_per_turn=payload.max_calls_per_turn,
    )
    return ApiResponse.ok(_policy(policy))
