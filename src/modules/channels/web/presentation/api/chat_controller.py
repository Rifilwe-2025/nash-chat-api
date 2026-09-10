"""The public chat API (spec §5.5, §5.6).

Authenticated by **API key only** — no user token, no tenant path parameter. The key determines the
tenant and the agent, so an integration cannot address anything but the agent it was issued for,
however the request is shaped. That is the isolation guarantee for this surface (§5.7).

These are the routes a tenant's own developer integrates against, so the documentation here is the
product: §10 requires that they can do it without reading the source or asking for help.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Path, Query
from fastapi.responses import StreamingResponse

from src.core.sse import format_json_event, sse_response
from src.modules.channels.domain.messages import IncomingMessage
from src.modules.channels.domain.models import ChannelType
from src.modules.channels.web.presentation.dependencies import (
    ChatChannelsDep,
    ChatConversationsDep,
    ChatReadDep,
    ChatWriteDep,
    StreamSettlerDep,
)
from src.modules.channels.web.presentation.dtos.chat import (
    ChatMessageResponse,
    ChatReplyResponse,
    ChatSessionResponse,
    SendChatMessageRequest,
)
from src.modules.conversations.domain.models import Channel, Message
from src.modules.conversations.domain.services import StreamedTurn
from src.shared.database.pagination import PageParamsDep
from src.shared.exceptions import NotFoundException
from src.shared.responses import ApiResponse, PaginatedResponse, create_router

router = create_router(prefix="/v1/chat", tags=["chat"])

ConversationIdPath = Annotated[uuid.UUID, Path(description="Identifier of the conversation.")]

UNAUTHORIZED = {
    "description": (
        "The API key is missing, invalid, revoked, or expired (`MISSING_API_KEY`, "
        "`INVALID_API_KEY`). Every one of these reads the same on purpose."
    )
}
FORBIDDEN = {
    "description": (
        "The key lacks the scope this route needs (`INSUFFICIENT_SCOPE`), the agent is no longer "
        "published (`AGENT_NOT_PUBLISHED`), or a browser sent the request from an origin the web "
        "channel does not allow (`ORIGIN_NOT_ALLOWED`)."
    )
}
UNAVAILABLE = {
    "description": (
        "The agent's model could not be reached (`PROVIDER_UNAVAILABLE`) or the web "
        "channel is disabled for this agent (`CHANNEL_DISABLED`)."
    )
}
INVALID_MESSAGE = {
    "description": "The message is empty or too long (`EMPTY_MESSAGE`, `MESSAGE_TOO_LONG`)."
}
RATE_LIMITED = {
    "description": (
        "The key's per-minute limit is exhausted (`RATE_LIMITED`). The response carries "
        "`Retry-After` and `X-RateLimit-Reset`; every response carries `X-RateLimit-Remaining`."
    )
}


def _message(message: Message) -> ChatMessageResponse:
    return ChatMessageResponse(
        id=message.id,
        role=message.role,
        content=message.content,
        created_at=message.created_at,
    )


async def _frames(turn: StreamedTurn) -> AsyncIterator[str]:
    """A ``delta`` per piece of the reply, then ``done`` — or ``error``, if it was cut short."""
    async for delta in turn.deltas:
        yield format_json_event({"delta": delta}, event="delta")

    if turn.failure is not None:
        yield format_json_event(
            {"code": turn.failure.code, "detail": turn.failure.detail}, event="error"
        )
        return

    yield format_json_event(
        {
            "done": True,
            "conversationId": str(turn.conversation.id),
            "escalated": turn.escalated,
        },
        event="done",
    )


@router.post(
    "/messages",
    response_model=ApiResponse[ChatReplyResponse],
    status_code=201,
    summary="Send a message and get the agent's reply",
    description=(
        "The endpoint your integration calls for every user message. Requires the `chat:write` "
        "scope.\n\n"
        "**Sessions.** Pass a stable `userId` — a signed-in user's id, or a random id you keep in "
        "the browser for anonymous visitors. Everything sent under one `userId` continues the same "
        "conversation, with history and context carried across turns. Send a different `userId` "
        "and you get a separate conversation.\n\n"
        "**Ordering.** Messages sent in quick succession for one `userId` are answered in order "
        "rather than racing, so you do not need to serialise them yourself.\n\n"
        "**Escalation.** When `escalated` is true, a guardrail has handed the conversation to a "
        "human. The agent will not answer further messages in it — start a new session, or wait "
        "for your team to take over. Subscribe to the `conversation.escalated` webhook to be told "
        "as it happens."
    ),
    responses={
        201: {"description": "The agent's reply."},
        401: UNAUTHORIZED,
        403: FORBIDDEN,
        409: UNAVAILABLE,
        422: INVALID_MESSAGE,
        429: RATE_LIMITED,
    },
)
async def send_message(
    payload: SendChatMessageRequest,
    caller: ChatWriteDep,
    channels: ChatChannelsDep,
    conversations: ChatConversationsDep,
) -> ApiResponse[ChatReplyResponse]:
    await channels.assert_channel_enabled(caller.agent.id, ChannelType.WEB)

    outgoing = await channels.handle(
        IncomingMessage(
            agent_id=caller.agent.id,
            channel=ChannelType.WEB.value,
            external_user_id=payload.user_id,
            text=payload.message,
        ),
        conversations,
    )

    return ApiResponse.ok(
        ChatReplyResponse(
            conversation_id=outgoing.conversation_id,
            reply=outgoing.text,
            escalated=outgoing.escalated,
        )
    )


@router.post(
    "/messages/stream",
    summary="Send a message and stream the reply",
    description=(
        "The same turn as `/v1/chat/messages`, streamed as **server-sent events** so a widget can "
        "render the reply as it is written. Requires the `chat:write` scope.\n\n"
        "Three kinds of frame:\n\n"
        '- `event: delta` — `{"delta": "..."}` for each piece of text, in order. Concatenating '
        "every delta gives the same reply the non-streaming endpoint returns.\n"
        '- `event: done` — `{"done": true, "conversationId": "...", "escalated": false}` once '
        "the reply is complete. `escalated` means what it does on `/v1/chat/messages`: a "
        "guardrail has handed the conversation to a human, and the agent will not answer "
        "further messages in it.\n"
        '- `event: error` — `{"code": "PROVIDER_UNAVAILABLE", "detail": "..."}` **instead of** '
        "`done`, when the model fails part-way. The deltas already sent are all there will be; "
        "sending the message again is safe.\n\n"
        "A request refused before streaming starts — a bad key, a disallowed origin, a disabled "
        "channel — gets the ordinary JSON error envelope and status code, never a stream.\n\n"
        "The conversation id is also sent in the `X-Conversation-Id` header, before the first "
        "frame.\n\n"
        "**The turn is still stored** — history, tokens and citations are recorded exactly as they "
        "are for a non-streamed message, and what was written is kept even when the caller "
        "disconnects part-way.\n\n"
        "A guardrail reply (a restricted topic, or an escalation) arrives as a single delta: there "
        "is no model call to stream, and the answer was already decided."
    ),
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "A `text/event-stream` of `delta` frames, ending in `done` or, if the model "
                "failed part-way, `error`."
            ),
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        401: UNAUTHORIZED,
        403: FORBIDDEN,
        409: UNAVAILABLE,
        422: INVALID_MESSAGE,
        429: RATE_LIMITED,
    },
)
async def stream_message(
    payload: SendChatMessageRequest,
    caller: ChatWriteDep,
    channels: ChatChannelsDep,
    conversations: ChatConversationsDep,
    settler: StreamSettlerDep,
) -> StreamingResponse:
    await channels.assert_channel_enabled(caller.agent.id, ChannelType.WEB)

    turn = await channels.handle_stream(
        IncomingMessage(
            agent_id=caller.agent.id,
            channel=ChannelType.WEB.value,
            external_user_id=payload.user_id,
            text=payload.message,
        ),
        conversations,
    )
    settler.turn = turn
    return sse_response(_frames(turn), headers={"X-Conversation-Id": str(turn.conversation.id)})


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=PaginatedResponse[ChatMessageResponse],
    summary="Fetch a conversation's history",
    description=(
        "Returns the messages in a conversation your key's agent owns, oldest first. Requires the "
        "`chat:read` scope.\n\n"
        "Use it to rehydrate a chat widget when someone returns to the page. Internal detail — "
        "token counts, costs, retrieval tiers — is not exposed here; the tenant console has it."
    ),
    responses={
        200: {"description": "A page of messages."},
        401: UNAUTHORIZED,
        403: FORBIDDEN,
        404: {
            "description": ("No such conversation for this key's agent (`CONVERSATION_NOT_FOUND`).")
        },
        429: RATE_LIMITED,
    },
)
async def get_history(
    conversation_id: ConversationIdPath,
    caller: ChatReadDep,
    conversations: ChatConversationsDep,
    page: PageParamsDep,
) -> PaginatedResponse[ChatMessageResponse]:
    conversation = await conversations.get(conversation_id)
    if conversation.agent_id != caller.agent.id:
        # A key speaks for one agent. Another agent's conversation is reported as missing rather
        # than forbidden, so a key cannot be used to probe the tenant's other agents.
        raise NotFoundException("Conversation does not exist.", code="CONVERSATION_NOT_FOUND")

    result = await conversations.transcript(conversation_id, page)
    return PaginatedResponse.of(
        items=[_message(message) for message in result.items],
        page=result.page,
        page_size=result.page_size,
        total_items=result.total,
    )


@router.get(
    "/session",
    response_model=ApiResponse[ChatSessionResponse],
    summary="Look up the open conversation for a user",
    description=(
        "Returns the conversation currently open for a `userId`, if there is one, so a widget can "
        "reconnect to it after a page reload. Requires the `chat:read` scope.\n\n"
        "`conversationId` is absent when that user has no open conversation — the next message "
        "they send will start one."
    ),
    responses={
        200: {"description": "The open conversation, if any."},
        401: UNAUTHORIZED,
        403: FORBIDDEN,
        429: RATE_LIMITED,
    },
)
async def get_session(
    caller: ChatReadDep,
    conversations: ChatConversationsDep,
    user_id: Annotated[
        str, Query(alias="userId", max_length=255, description="The session key you send.")
    ],
) -> ApiResponse[ChatSessionResponse]:
    conversation = await conversations.conversations.find_open_session(
        caller.agent.id, Channel.WEB, user_id
    )
    return ApiResponse.ok(
        ChatSessionResponse(
            conversation_id=conversation.id if conversation else None,
            status=conversation.status if conversation else None,
        )
    )
