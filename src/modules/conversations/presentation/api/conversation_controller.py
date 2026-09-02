from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Path, Query

from src.modules.conversations.domain.models import (
    Channel,
    Conversation,
    ConversationStatus,
    Message,
)
from src.modules.conversations.domain.services import ConversationService, TurnResult
from src.modules.conversations.presentation.dtos.conversation import (
    ConversationDetailResponse,
    ConversationResponse,
    ConversationSummaryResponse,
    ConversationUsageResponse,
    EscalateRequest,
    MessageResponse,
    SendMessageRequest,
    TurnResponse,
    citations_of,
)
from src.modules.tenants.presentation.dependencies import CurrentTenantDep
from src.modules.tools.presentation.dependencies import ToolCacheDep
from src.shared.database.dependencies import SessionDep
from src.shared.database.pagination import PageParamsDep
from src.shared.responses import ApiResponse, PaginatedResponse, create_router

router = create_router(prefix="/conversations", tags=["conversations"])


def get_conversation_service(
    session: SessionDep, tenant_id: CurrentTenantDep, tool_cache: ToolCacheDep
) -> ConversationService:
    """The tenant comes from the token, so every query below is scoped before it is written."""
    return ConversationService(session, tenant_id, tool_cache=tool_cache)


ServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]
ConversationIdPath = Annotated[uuid.UUID, Path(description="Identifier of the conversation.")]

UNAUTHORIZED = {
    "description": "Access token is missing, invalid, or revoked (`UNAUTHORIZED`, `INVALID_TOKEN`)."
}
NOT_FOUND = {
    "description": (
        "No such conversation in your tenant (`CONVERSATION_NOT_FOUND`). Another tenant's "
        "conversation is reported as missing rather than forbidden."
    )
}


def _message(message: Message) -> MessageResponse:
    return MessageResponse(
        id=message.id,
        role=message.role,
        content=message.content,
        provider=message.provider,
        model=message.model,
        prompt_tokens=message.prompt_tokens,
        completion_tokens=message.completion_tokens,
        cost_micro_usd=message.cost_micro_usd,
        citations=citations_of(message.citations_json),
        created_at=message.created_at,
    )


def _conversation(conversation: Conversation) -> ConversationResponse:
    return ConversationResponse(
        id=conversation.id,
        tenant_id=conversation.tenant_id,
        agent_id=conversation.agent_id,
        channel=conversation.channel,
        external_user_id=conversation.external_user_id,
        status=conversation.status,
        summary=conversation.summary,
        escalated_at=conversation.escalated_at,
        escalation_reason=conversation.escalation_reason,
        last_message_at=conversation.last_message_at,
        created_at=conversation.created_at,
    )


def _summary(conversation: Conversation) -> ConversationSummaryResponse:
    return ConversationSummaryResponse(
        id=conversation.id,
        agent_id=conversation.agent_id,
        channel=conversation.channel,
        external_user_id=conversation.external_user_id,
        status=conversation.status,
        last_message_at=conversation.last_message_at,
    )


def _turn(result: TurnResult) -> TurnResponse:
    return TurnResponse(
        conversation_id=result.conversation.id,
        status=result.conversation.status,
        reply=_message(result.reply),
        escalated=result.escalated,
        retrieval_tier=result.retrieval.tier if result.retrieval else None,
        used_knowledge=bool(result.retrieval and result.retrieval.has_context),
    )


@router.post(
    "/messages",
    response_model=ApiResponse[TurnResponse],
    status_code=201,
    summary="Send a message to an agent",
    description=(
        "Runs one full turn — retrieval, prompt assembly, the provider call — and stores both "
        "sides of it. This is the builder's **preview chat**: it talks to an agent in any status, "
        "so a draft can be tested before it is published. The public, API-key-authenticated "
        "channel for real end users is separate.\n\n"
        "Sessions are keyed by agent, channel and `externalUserId`. Omit `conversationId` and the "
        "open session is continued, or a new one started; messages arriving together in one "
        "session are answered in order rather than racing.\n\n"
        "**Guardrails are enforced before the model is called.** A message matching an escalation "
        "trigger hands the conversation to a human and the agent stops answering; a message "
        "matching a restricted topic is declined without a provider call at all. Retrieved "
        "knowledge and the message itself are passed to the model as data, never as instructions."
    ),
    responses={
        201: {"description": "The turn completed and both messages were stored."},
        401: UNAUTHORIZED,
        404: {
            "description": "No such agent or conversation (`AGENT_NOT_FOUND`, "
            "`CONVERSATION_NOT_FOUND`)."
        },
        409: {
            "description": (
                "The conversation is closed or escalated (`CONVERSATION_NOT_ACTIVE`), the agent is "
                "not serving traffic (`AGENT_NOT_PUBLISHED`), or the provider could not be reached "
                "(`PROVIDER_UNAVAILABLE`)."
            )
        },
        422: {
            "description": (
                "The message is empty or too long (`EMPTY_MESSAGE`, `MESSAGE_TOO_LONG`), or the "
                "agent has no model configured (`AGENT_NOT_CONFIGURED`)."
            )
        },
    },
)
async def send_message(
    payload: SendMessageRequest, service: ServiceDep
) -> ApiResponse[TurnResponse]:
    result = await service.send_message(
        agent_id=payload.agent_id,
        content=payload.message,
        channel=Channel.PREVIEW,
        external_user_id=payload.external_user_id or "preview",
        conversation_id=payload.conversation_id,
    )
    return ApiResponse.ok(_turn(result))


@router.get(
    "",
    response_model=PaginatedResponse[ConversationSummaryResponse],
    summary="List conversations",
    description=(
        "Lists your tenant's conversations, most recently active first. Filter by `agentId`, by "
        "`status` to find escalated conversations waiting on a human, or by `channel` to separate "
        "builder test threads from real customer traffic.\n\n"
        "`channel=preview` is how the builder finds the agent's own test conversation: without it, "
        "the most recent thread for an agent is whichever channel spoke last, which may well be a "
        "live customer."
    ),
    responses={
        200: {"description": "A page of conversations."},
        401: UNAUTHORIZED,
        404: {"description": "No such agent in your tenant (`AGENT_NOT_FOUND`)."},
    },
)
async def list_conversations(
    service: ServiceDep,
    page: PageParamsDep,
    agent_id: Annotated[
        uuid.UUID | None, Query(alias="agentId", description="Only this agent's conversations.")
    ] = None,
    status: Annotated[
        ConversationStatus | None, Query(description="Only conversations in this state.")
    ] = None,
    channel: Annotated[
        Channel | None,
        Query(description="Only conversations on this channel, such as `preview` or `whatsapp`."),
    ] = None,
) -> PaginatedResponse[ConversationSummaryResponse]:
    result = await service.list_conversations(
        page, agent_id=agent_id, status=status, channel=channel
    )
    return PaginatedResponse.of(
        items=[_summary(conversation) for conversation in result.items],
        page=result.page,
        page_size=result.page_size,
        total_items=result.total,
    )


@router.get(
    "/{conversation_id}",
    response_model=ApiResponse[ConversationDetailResponse],
    summary="Get a conversation",
    description=(
        "Returns one conversation with its rolling summary and its token and cost totals. "
        "Fetch the transcript separately for the messages themselves."
    ),
    responses={200: {"description": "The conversation."}, 401: UNAUTHORIZED, 404: NOT_FOUND},
)
async def get_conversation(
    conversation_id: ConversationIdPath, service: ServiceDep
) -> ApiResponse[ConversationDetailResponse]:
    conversation = await service.get(conversation_id)
    prompt_tokens, completion_tokens, cost = await service.usage(conversation_id)
    return ApiResponse.ok(
        ConversationDetailResponse(
            conversation=_conversation(conversation),
            usage=ConversationUsageResponse(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                cost_micro_usd=cost,
            ),
        )
    )


@router.get(
    "/{conversation_id}/messages",
    response_model=PaginatedResponse[MessageResponse],
    summary="Get a conversation transcript",
    description=(
        "Returns the messages in a conversation, oldest first, with the tokens and cost each one "
        "used and the sources each answer was grounded in.\n\n"
        "`summary` rows appear inline where history was trimmed, so you can see what the model was "
        "actually told rather than only what was said."
    ),
    responses={200: {"description": "A page of messages."}, 401: UNAUTHORIZED, 404: NOT_FOUND},
)
async def get_transcript(
    conversation_id: ConversationIdPath, service: ServiceDep, page: PageParamsDep
) -> PaginatedResponse[MessageResponse]:
    result = await service.transcript(conversation_id, page)
    return PaginatedResponse.of(
        items=[_message(message) for message in result.items],
        page=result.page,
        page_size=result.page_size,
        total_items=result.total,
    )


@router.post(
    "/{conversation_id}/escalate",
    response_model=ApiResponse[ConversationResponse],
    summary="Hand a conversation to a human",
    description=(
        "Marks the conversation as **escalated**. The agent stops answering it: an escalated "
        "conversation is no longer an open session, so the customer's next message starts a fresh "
        "one rather than the agent talking over whoever picked it up.\n\n"
        "Escalating an already-escalated conversation changes nothing."
    ),
    responses={
        200: {"description": "The conversation is escalated."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
    },
)
async def escalate(
    conversation_id: ConversationIdPath, payload: EscalateRequest, service: ServiceDep
) -> ApiResponse[ConversationResponse]:
    conversation = await service.escalate(conversation_id, reason=payload.reason)
    return ApiResponse.ok(_conversation(conversation), message="Conversation escalated.")


@router.post(
    "/{conversation_id}/close",
    response_model=ApiResponse[ConversationResponse],
    summary="Close a conversation",
    description=(
        "Ends the session. History is kept, but the conversation is no longer continued — the same "
        "customer messaging again starts a new one with fresh context."
    ),
    responses={
        200: {"description": "The conversation is closed."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
    },
)
async def close(
    conversation_id: ConversationIdPath, service: ServiceDep
) -> ApiResponse[ConversationResponse]:
    return ApiResponse.ok(
        _conversation(await service.close(conversation_id)), message="Conversation closed."
    )
