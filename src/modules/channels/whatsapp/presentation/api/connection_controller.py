"""The tenant-facing WhatsApp surface: connect a number, send to it, read the log (spec §5.5).

Authenticated by the user's access token, so everything here is scoped to the caller's own tenant.
The public half of the channel — the webhook Meta calls — is in ``webhook_controller.py`` and shares
nothing with these routes but the service beneath them.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Path, Query, Request

from src import configs
from src.modules.channels.domain.models import ChannelConfig
from src.modules.channels.whatsapp.domain.models import (
    DeliveryStatus,
    MessageDirection,
    WhatsAppMessage,
)
from src.modules.channels.whatsapp.domain.services import OutboundMedia
from src.modules.channels.whatsapp.internal import connection as connection_fields
from src.modules.channels.whatsapp.internal.providers import InboundKind, TemplateMessage
from src.modules.channels.whatsapp.internal.session_window import SessionWindow
from src.modules.channels.whatsapp.presentation.dependencies import ServiceDep
from src.modules.channels.whatsapp.presentation.dtos.whatsapp import (
    ConnectWhatsAppRequest,
    SendWhatsAppRequest,
    SessionWindowResponse,
    WhatsAppConnectionResponse,
    WhatsAppMediaRequest,
    WhatsAppMessageResponse,
    WhatsAppTemplateRequest,
)
from src.shared.database.pagination import PageParamsDep
from src.shared.responses import ApiResponse, PaginatedResponse, create_router

router = create_router(tags=["whatsapp"])

AgentIdPath = Annotated[uuid.UUID, Path(description="Identifier of the agent.")]
ContactIdPath = Annotated[
    str,
    Path(
        description="The contact's WhatsApp id — E.164 without the '+'.",
        examples=["263770000000"],
        max_length=64,
    ),
]

UNAUTHORIZED = {
    "description": "Access token is missing, invalid, or revoked (`UNAUTHORIZED`, `INVALID_TOKEN`)."
}
AGENT_NOT_FOUND = {"description": "No such agent in your tenant (`AGENT_NOT_FOUND`)."}
NOT_CONNECTED = {
    "description": (
        "No such agent, or it has no WhatsApp connection (`AGENT_NOT_FOUND`, "
        "`CHANNEL_NOT_CONFIGURED`)."
    )
}


def webhook_url(request: Request, connection_id: uuid.UUID) -> str:
    """The callback URL a tenant pastes into Meta.

    Built from ``WHATSAPP_PUBLIC_BASE_URL`` when it is set, because behind a proxy the request's own
    origin is the internal one — and a tenant pasting ``http://api:8000/…`` into Meta gets a
    subscription that can never be verified.
    """
    base = str(configs.WHATSAPP_PUBLIC_BASE_URL or "").rstrip("/") or str(request.base_url).rstrip(
        "/"
    )
    return f"{base}/v1/channels/whatsapp/webhook/{connection_id}"


def _connection(
    request: Request, config: ChannelConfig, reveal_verify_token: bool = False
) -> WhatsAppConnectionResponse:
    credentials = dict(config.credentials_json)
    return WhatsAppConnectionResponse(
        id=config.id,
        agent_id=config.agent_id,
        status=config.status,
        credentials=connection_fields.redact(credentials),
        settings=dict(config.settings_json),
        webhook_url=webhook_url(request, config.id),
        verify_token=(
            str(credentials.get(connection_fields.VERIFY_TOKEN) or "")
            if reveal_verify_token
            else None
        ),
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


def _message(message: WhatsAppMessage) -> WhatsAppMessageResponse:
    return WhatsAppMessageResponse(
        id=message.id,
        direction=message.direction,
        status=message.status,
        message_type=message.message_type,
        contact_id=message.wa_contact_id,
        body=message.body,
        template_name=message.template_name,
        provider_message_id=message.provider_message_id,
        conversation_id=message.conversation_id,
        error_detail=message.error_detail,
        sent_at=message.sent_at,
        delivered_at=message.delivered_at,
        read_at=message.read_at,
        created_at=message.created_at,
    )


def _window(contact_id: str, window: SessionWindow, config: ChannelConfig) -> SessionWindowResponse:
    configured = connection_fields.template_from(dict(config.settings_json))
    return SessionWindowResponse(
        contact_id=contact_id,
        is_open=window.is_open,
        last_inbound_at=window.last_inbound_at,
        expires_at=window.expires_at,
        seconds_remaining=window.seconds_remaining,
        fallback_template=configured[0] if configured else None,
    )


def _media(payload: WhatsAppMediaRequest | None) -> OutboundMedia | None:
    if payload is None:
        return None
    return OutboundMedia(url=payload.url, kind=InboundKind(payload.kind), caption=payload.caption)


def _template(payload: WhatsAppTemplateRequest | None) -> TemplateMessage | None:
    if payload is None:
        return None
    return TemplateMessage(
        name=payload.name, language=payload.language, variables=list(payload.variables)
    )


# -- connection ----------------------------------------------------------------------


@router.put(
    "/agents/{agent_id}/channels/whatsapp",
    response_model=ApiResponse[WhatsAppConnectionResponse],
    summary="Connect an agent to a WhatsApp number",
    description=(
        "Stores the credentials for one WhatsApp Business number and returns the two values you "
        "still need to paste into Meta: the **callback URL** and the **verify token**.\n\n"
        "**The order matters.** Call this first, then open your Meta app → WhatsApp → "
        "Configuration, paste both values, and click *Verify and save*. Meta immediately calls the "
        "callback URL with the token, which only matches once this connection exists.\n\n"
        "Call it again to rotate a credential — anything you omit is left as it was, so changing "
        "an access token does not mean re-pasting the app secret. The verify token is issued once "
        "and returned on every call to this route, since you may need it again if you "
        "re-verify.\n\n"
        "Secrets go in and never come back: reads return `hasAccessToken` and `hasAppSecret` "
        "instead of the values."
    ),
    responses={
        200: {"description": "The connection, with the setup values."},
        401: UNAUTHORIZED,
        404: AGENT_NOT_FOUND,
        422: {
            "description": (
                "A credential this provider requires is missing "
                "(`WHATSAPP_INCOMPLETE_CREDENTIALS`), or the provider is not one we support "
                "(`WHATSAPP_UNKNOWN_PROVIDER`)."
            )
        },
    },
)
async def connect_whatsapp(
    agent_id: AgentIdPath,
    payload: ConnectWhatsAppRequest,
    request: Request,
    service: ServiceDep,
) -> ApiResponse[WhatsAppConnectionResponse]:
    supplied = {
        connection_fields.PROVIDER: payload.provider,
        connection_fields.PHONE_NUMBER_ID: payload.phone_number_id,
        connection_fields.ACCESS_TOKEN: payload.access_token,
        connection_fields.APP_SECRET: payload.app_secret,
        connection_fields.BUSINESS_ACCOUNT_ID: payload.business_account_id,
        connection_fields.DISPLAY_PHONE_NUMBER: payload.display_phone_number,
    }
    settings: dict[str, object] = {
        connection_fields.AUTO_REPLY: payload.auto_reply,
        connection_fields.MARK_READ: payload.mark_read,
    }
    if payload.outside_window_template is not None:
        settings[connection_fields.TEMPLATE] = {
            connection_fields.TEMPLATE_NAME: payload.outside_window_template.name,
            connection_fields.TEMPLATE_LANGUAGE: payload.outside_window_template.language,
            connection_fields.TEMPLATE_VARIABLES: list(payload.outside_window_template.variables),
        }

    config = await service.connect(
        agent_id,
        # An omitted field means "leave it", so nulls are dropped before they reach the merge.
        credentials={key: value for key, value in supplied.items() if value is not None},
        settings=settings,
        status=payload.status,
    )
    return ApiResponse.ok(
        _connection(request, config, reveal_verify_token=True),
        message="WhatsApp connection saved. Paste the webhook URL and verify token into Meta.",
    )


@router.get(
    "/agents/{agent_id}/channels/whatsapp",
    response_model=ApiResponse[WhatsAppConnectionResponse],
    summary="Get an agent's WhatsApp connection",
    description=(
        "The current connection and its webhook URL. Credentials come back redacted — "
        "`hasAccessToken` and `hasAppSecret` tell you the connection is complete without returning "
        "the secrets themselves. The verify token is not readable here; re-save the connection if "
        "you need it again."
    ),
    responses={
        200: {"description": "The connection."},
        401: UNAUTHORIZED,
        404: NOT_CONNECTED,
    },
)
async def get_whatsapp_connection(
    agent_id: AgentIdPath, request: Request, service: ServiceDep
) -> ApiResponse[WhatsAppConnectionResponse]:
    return ApiResponse.ok(_connection(request, await service.get_connection(agent_id)))


@router.delete(
    "/agents/{agent_id}/channels/whatsapp",
    response_model=ApiResponse[None],
    summary="Disconnect an agent from WhatsApp",
    description=(
        "Deletes the connection and its stored credentials. Inbound webhooks stop resolving "
        "immediately and Meta's deliveries will start failing, so remove the callback URL in your "
        "Meta app too. The message log is kept — it belongs to your history, not to the connection."
    ),
    responses={
        200: {"description": "The connection was removed."},
        401: UNAUTHORIZED,
        404: NOT_CONNECTED,
    },
)
async def disconnect_whatsapp(agent_id: AgentIdPath, service: ServiceDep) -> ApiResponse[None]:
    await service.disconnect(agent_id)
    return ApiResponse.ok(message="WhatsApp connection removed.")


# -- messaging -----------------------------------------------------------------------


@router.post(
    "/agents/{agent_id}/channels/whatsapp/messages",
    response_model=ApiResponse[WhatsAppMessageResponse],
    status_code=201,
    summary="Send a WhatsApp message to a contact",
    description=(
        "Sends a message from this agent's number. Use it for anything your agent is not answering "
        "on its own — an order update, a human taking over an escalated conversation.\n\n"
        "**The 24-hour rule.** WhatsApp only delivers free-form text within 24 hours of the "
        "contact's last message to you. Inside that window `text` is sent as written. Outside it, "
        "your connection's `outsideWindowTemplate` is sent instead — and if you have not "
        "configured one, the send is refused with `WHATSAPP_WINDOW_CLOSED` rather than silently "
        "dropped. Check `GET …/sessions/{contactId}` first if you need to know in advance.\n\n"
        "An explicit `template` is always delivered, window or not, because a template is what "
        "WhatsApp permits outside one.\n\n"
        "**Sending a file.** Give `media` a public HTTPS URL and its kind; WhatsApp downloads it "
        "from you. It counts as free-form, so it obeys the same window rule as `text`."
    ),
    responses={
        201: {"description": "The message was accepted by WhatsApp."},
        401: UNAUTHORIZED,
        404: NOT_CONNECTED,
        409: {
            "description": (
                "The window has closed and no fallback template is configured "
                "(`WHATSAPP_WINDOW_CLOSED`), WhatsApp rejected the message "
                "(`WHATSAPP_SEND_REJECTED`), or the connection is missing a credential "
                "(`WHATSAPP_CONNECTION_INCOMPLETE`)."
            )
        },
        422: {
            "description": (
                "Nothing to send — no text, media, or template (`WHATSAPP_NOTHING_TO_SEND`)."
            )
        },
        503: {
            "description": "WhatsApp could not be reached (`WHATSAPP_PROVIDER_UNAVAILABLE`). Retry."
        },
    },
)
async def send_whatsapp_message(
    agent_id: AgentIdPath, payload: SendWhatsAppRequest, service: ServiceDep
) -> ApiResponse[WhatsAppMessageResponse]:
    message = await service.send_to_agent_contact(
        agent_id,
        payload.to,
        text=payload.text,
        template=_template(payload.template),
        media=_media(payload.media),
    )
    return ApiResponse.ok(_message(message))


@router.get(
    "/agents/{agent_id}/channels/whatsapp/messages",
    response_model=PaginatedResponse[WhatsAppMessageResponse],
    summary="List WhatsApp messages and their delivery status",
    description=(
        "The delivery log for this agent's number, newest first — both directions, with what "
        "became of each message.\n\n"
        "This is where a message that failed explains itself: `status` is `failed` and "
        "`errorDetail` carries WhatsApp's own reason. Filter by `direction`, `status`, or "
        "`contactId` to narrow it."
    ),
    responses={
        200: {"description": "A page of messages."},
        401: UNAUTHORIZED,
        404: NOT_CONNECTED,
    },
)
async def list_whatsapp_messages(
    agent_id: AgentIdPath,
    service: ServiceDep,
    page: PageParamsDep,
    direction: Annotated[
        MessageDirection | None, Query(description="Only inbound, or only outbound.")
    ] = None,
    status: Annotated[
        DeliveryStatus | None, Query(description="Only messages in this delivery state.")
    ] = None,
    contact_id: Annotated[
        str | None,
        Query(alias="contactId", max_length=64, description="Only this contact's messages."),
    ] = None,
) -> PaginatedResponse[WhatsAppMessageResponse]:
    result = await service.list_messages(
        agent_id, page, direction=direction, status=status, contact_id=contact_id
    )
    return PaginatedResponse.of(
        items=[_message(message) for message in result.items],
        page=result.page,
        page_size=result.page_size,
        total_items=result.total,
    )


@router.get(
    "/agents/{agent_id}/channels/whatsapp/sessions/{contact_id}",
    response_model=ApiResponse[SessionWindowResponse],
    summary="Check a contact's 24-hour session window",
    description=(
        "Whether free-form text will reach this contact right now, and how long you have left.\n\n"
        "`isOpen` is true while the contact's 24-hour customer service window is open — it opens "
        "each time they message you. When it is false, only a template is delivered, and "
        "`fallbackTemplate` names the one that will be used. A `null` there means a send outside "
        "the window will be refused.\n\n"
        "A contact who has never messaged this number has no window at all: `lastInboundAt` is "
        "null and only a template can reach them."
    ),
    responses={
        200: {"description": "The contact's window."},
        401: UNAUTHORIZED,
        404: NOT_CONNECTED,
    },
)
async def get_session_window(
    agent_id: AgentIdPath, contact_id: ContactIdPath, service: ServiceDep
) -> ApiResponse[SessionWindowResponse]:
    connection = await service.get_connection(agent_id)
    window = await service.window_for(connection, contact_id)
    return ApiResponse.ok(_window(contact_id, window, connection))
