from __future__ import annotations

import uuid
from functools import partial
from typing import Annotated

import anyio
from fastapi import Depends, Path, Query, Request, Response

from src import configs
from src.core.public_url import public_base_url
from src.modules.agents.domain.models import Agent
from src.modules.api_keys.domain.services import ApiKeyService
from src.modules.channels.domain.models import ChannelConfig, ChannelType, WebhookEndpoint
from src.modules.channels.domain.services import ChannelService
from src.modules.channels.internal import integration_docs
from src.modules.channels.presentation.dtos.channel import (
    ChannelConfigResponse,
    ConfigureChannelRequest,
    CreateWebhookRequest,
    DocsFormat,
    IntegrationDocsResponse,
    UpdateWebhookRequest,
    WebhookResponse,
    WebhookTestResponse,
)
from src.modules.tenants.presentation.dependencies import CurrentTenantDep
from src.shared.database.dependencies import SessionDep
from src.shared.database.pagination import PageParamsDep
from src.shared.documents import markdown_pdf
from src.shared.exceptions import ConflictException, NotFoundException
from src.shared.responses import ApiResponse, PaginatedResponse, create_router

router = create_router(tags=["channels"])


def get_channel_service(session: SessionDep, tenant_id: CurrentTenantDep) -> ChannelService:
    """The tenant comes from the token, so every query below is scoped before it is written."""
    return ChannelService(session, tenant_id)


ServiceDep = Annotated[ChannelService, Depends(get_channel_service)]
WebhookIdPath = Annotated[uuid.UUID, Path(description="Identifier of the webhook endpoint.")]
AgentIdPath = Annotated[uuid.UUID, Path(description="Identifier of the agent.")]

UNAUTHORIZED = {
    "description": "Access token is missing, invalid, or revoked (`UNAUTHORIZED`, `INVALID_TOKEN`)."
}
WEBHOOK_NOT_FOUND = {
    "description": "No such webhook endpoint in your tenant (`WEBHOOK_NOT_FOUND`)."
}


def _webhook(endpoint: WebhookEndpoint) -> WebhookResponse:
    return WebhookResponse(
        id=endpoint.id,
        agent_id=endpoint.agent_id,
        url=endpoint.url,
        events=[str(event) for event in endpoint.events],
        status=endpoint.status,
        secret=endpoint.secret,
        failure_count=endpoint.failure_count,
        last_delivery_at=endpoint.last_delivery_at,
        last_error=endpoint.last_error,
        created_at=endpoint.created_at,
    )


def _config(config: ChannelConfig) -> ChannelConfigResponse:
    return ChannelConfigResponse(
        id=config.id,
        agent_id=config.agent_id,
        channel_type=config.channel_type,
        status=config.status,
        settings=config.settings_json,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


# -- integration documentation -----------------------------------------------------

MEDIA_TYPES = {
    DocsFormat.MARKDOWN: "text/markdown; charset=utf-8",
    DocsFormat.PDF: "application/pdf",
}

ApiKeyIdQuery = Annotated[
    uuid.UUID | None,
    Query(alias="apiKeyId", description="Use this key's prefix, scopes and limit."),
]
DOCS_NOT_FOUND = {"description": "No such agent or key (`AGENT_NOT_FOUND`, `API_KEY_NOT_FOUND`)."}


async def _guide(
    agent_id: uuid.UUID,
    request: Request,
    service: ChannelService,
    session: SessionDep,
    tenant_id: uuid.UUID,
    api_key_id: uuid.UUID | None,
) -> tuple[Agent, str, str]:
    """The agent, the base URL the guide was written for, and the guide itself.

    Shared by both routes, so what a tenant downloads cannot drift from what they read on screen:
    it is the same Markdown, rendered differently.
    """
    agent = await service.agents.get(agent_id)

    prefix, scopes, rate_limit = (
        "nsk_live_xxx",
        ["chat:write", "chat:read"],
        configs.RATE_LIMIT_DEFAULT_PER_MINUTE,
    )
    if api_key_id is not None:
        api_key = await ApiKeyService(session, tenant_id).get(api_key_id)
        if api_key.agent_id != agent.id:
            raise NotFoundException("API key does not exist.", code="API_KEY_NOT_FOUND")
        prefix, scopes, rate_limit = (
            api_key.prefix,
            [str(scope) for scope in api_key.scopes],
            api_key.rate_limit_per_minute,
        )

    # `PUBLIC_BASE_URL` where it is set, because behind a proxy the request's own origin is the
    # internal one — and this URL gets copied into somebody's code, or into a PDF that outlives
    # the request entirely.
    base_url = public_base_url(request)

    # The WhatsApp section appears only when a number is actually connected. Read through the
    # channel service rather than the WhatsApp module's own, because this controller belongs to
    # `channels` and cross-module access is service to service.
    whatsapp = await service.configs.for_agent(agent.id, ChannelType.WHATSAPP)

    markdown = integration_docs.build(
        agent_name=agent.name,
        agent_id=str(agent.id),
        base_url=base_url,
        key_prefix=prefix,
        scopes=scopes,
        rate_limit=rate_limit,
        signature_header=configs.WEBHOOKS_SIGNATURE_HEADER,
        schema=request.app.openapi(),
        allowed_origins=await service.allowed_origins(agent.id),
        whatsapp_connection_id=str(whatsapp.id) if whatsapp else None,
        whatsapp_phone_number_id=(
            str(whatsapp.credentials_json.get("phoneNumberId") or "") if whatsapp else None
        ),
    )
    return agent, base_url, markdown


@router.get(
    "/agents/{agent_id}/integration-docs",
    response_model=ApiResponse[IntegrationDocsResponse],
    summary="Get an agent's integration guide",
    description=(
        "Returns a complete integration guide for this agent, in Markdown — quickstart, where the "
        "key belongs, the session model, the streaming frames, resuming a conversation, escalation "
        "handling, allowed origins, webhook verification, rate limits, error codes, and every "
        "public endpoint.\n\n"
        "**Generated from the schema this API is currently serving**, not written by hand, so it "
        "cannot describe a route that no longer exists or miss one that was added. Hand it to "
        "whoever is doing the integration, or send them the file from "
        "`GET /agents/{agentId}/integration-docs/export`.\n\n"
        "Pass `apiKeyId` to have the guide use that key's real prefix, scopes and rate limit "
        "instead of placeholders."
    ),
    responses={
        200: {"description": "The generated guide."},
        401: UNAUTHORIZED,
        404: DOCS_NOT_FOUND,
    },
)
async def get_integration_docs(
    agent_id: AgentIdPath,
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    tenant_id: CurrentTenantDep,
    api_key_id: ApiKeyIdQuery = None,
) -> ApiResponse[IntegrationDocsResponse]:
    agent, base_url, markdown = await _guide(
        agent_id, request, service, session, tenant_id, api_key_id
    )
    return ApiResponse.ok(
        IntegrationDocsResponse(
            agent_id=agent.id, agent_name=agent.name, base_url=base_url, markdown=markdown
        )
    )


@router.get(
    "/agents/{agent_id}/integration-docs/export",
    summary="Download an agent's integration guide",
    description=(
        "The same guide as `GET /agents/{agentId}/integration-docs`, as a file to keep or send "
        "on: `format=md` for the Markdown, `format=pdf` for a PDF whose text is still text — "
        "selectable, searchable, and copy-pasteable out of the code blocks.\n\n"
        "Both are safe to share: only the key's **prefix** ever appears in them, never the "
        "secret.\n\n"
        "The body is the document itself rather than the usual JSON envelope, named by a "
        "`Content-Disposition` filename such as `integrating-sales-assistant.pdf`."
    ),
    response_class=Response,
    responses={
        200: {
            "description": "The guide as a file.",
            "content": {
                "text/markdown": {"schema": {"type": "string"}},
                "application/pdf": {"schema": {"type": "string", "format": "binary"}},
            },
        },
        401: UNAUTHORIZED,
        404: DOCS_NOT_FOUND,
    },
)
async def export_integration_docs(
    agent_id: AgentIdPath,
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    tenant_id: CurrentTenantDep,
    docs_format: Annotated[
        DocsFormat, Query(alias="format", description="`md` for Markdown, `pdf` for a PDF.")
    ] = DocsFormat.MARKDOWN,
    api_key_id: ApiKeyIdQuery = None,
) -> Response:
    agent, _, markdown = await _guide(agent_id, request, service, session, tenant_id, api_key_id)

    if docs_format is DocsFormat.PDF:
        # Laying out a PDF is CPU work, and this process is also streaming replies to other
        # people's customers. It goes to a worker thread rather than holding the event loop.
        body = await anyio.to_thread.run_sync(
            partial(markdown_pdf.render, markdown, title=f"Integrating {agent.name}")
        )
    else:
        body = markdown.encode("utf-8")

    filename = integration_docs.filename(agent.name, docs_format.value)
    return Response(
        content=body,
        media_type=MEDIA_TYPES[docs_format],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -- channel configuration ----------------------------------------------------------


@router.put(
    "/agents/{agent_id}/channels/{channel_type}",
    response_model=ApiResponse[ChannelConfigResponse],
    summary="Configure a channel for an agent",
    description=(
        "Creates or updates this agent's settings for one channel. A channel with no configuration "
        "is **open** — a published agent answers on the web channel out of the box — so this is "
        "for narrowing behaviour rather than switching it on.\n\n"
        "**WhatsApp is not configured here.** It needs validated credentials and issues a verify "
        "token, neither of which an untyped settings blob can do — use "
        "`PUT /agents/{agentId}/channels/whatsapp` instead."
    ),
    responses={
        200: {"description": "The channel configuration."},
        401: UNAUTHORIZED,
        404: {"description": "No such agent in your tenant (`AGENT_NOT_FOUND`)."},
        409: {
            "description": (
                "This channel has a dedicated connection endpoint (`CHANNEL_NEEDS_SETUP_ROUTE`)."
            )
        },
    },
)
async def configure_channel(
    agent_id: AgentIdPath,
    channel_type: Annotated[ChannelType, Path(description="Which channel to configure.")],
    payload: ConfigureChannelRequest,
    service: ServiceDep,
) -> ApiResponse[ChannelConfigResponse]:
    if channel_type is ChannelType.WHATSAPP:
        # Refused rather than accepted-and-ignored. WhatsApp credentials are validated on the way
        # in and the connection issues a verify token; letting this route write an unchecked blob
        # would produce a connection that looks saved and can never receive a webhook.
        raise ConflictException(
            "Configure WhatsApp through PUT /agents/{agentId}/channels/whatsapp, which validates "
            "the credentials and issues your webhook URL and verify token.",
            code="CHANNEL_NEEDS_SETUP_ROUTE",
        )

    config = await service.configure(
        agent_id,
        channel_type,
        settings=payload.settings,
        credentials=payload.credentials,
    )
    return ApiResponse.ok(_config(config))


@router.get(
    "/agents/{agent_id}/channels",
    response_model=ApiResponse[list[ChannelConfigResponse]],
    summary="List an agent's channel configurations",
    description=(
        "Returns the channels explicitly configured for this agent. Channels absent from the list "
        "are unconfigured, which means open rather than off. Credentials are never returned."
    ),
    responses={
        200: {"description": "The agent's channel configurations."},
        401: UNAUTHORIZED,
        404: {"description": "No such agent in your tenant (`AGENT_NOT_FOUND`)."},
    },
)
async def list_channels(
    agent_id: AgentIdPath, service: ServiceDep
) -> ApiResponse[list[ChannelConfigResponse]]:
    configs_for_agent = await service.list_configs(agent_id)
    return ApiResponse.ok([_config(config) for config in configs_for_agent])


# -- webhooks -----------------------------------------------------------------------


@router.post(
    "/webhooks",
    response_model=ApiResponse[WebhookResponse],
    status_code=201,
    summary="Create a webhook endpoint",
    description=(
        "Subscribes a URL to platform events. The response contains the **signing secret** — every "
        "delivery is signed with it, and your receiver must verify that signature: a webhook URL "
        "is not a secret, and anyone who guesses yours can post to it.\n\n"
        "Deliveries are best effort and are not retried yet, so treat an event as a prompt to act "
        "rather than the only record."
    ),
    responses={
        201: {"description": "The endpoint was created."},
        401: UNAUTHORIZED,
        404: {"description": "No such agent in your tenant (`AGENT_NOT_FOUND`)."},
        422: {
            "description": (
                "An unknown event (`UNKNOWN_WEBHOOK_EVENT`) or none at all "
                "(`WEBHOOK_NEEDS_EVENT`)."
            )
        },
    },
)
async def create_webhook(
    payload: CreateWebhookRequest, service: ServiceDep
) -> ApiResponse[WebhookResponse]:
    endpoint = await service.create_endpoint(
        url=payload.url,
        events=[event.value for event in payload.events],
        agent_id=payload.agent_id,
    )
    return ApiResponse.ok(_webhook(endpoint), message="Webhook endpoint created.")


@router.get(
    "/webhooks",
    response_model=PaginatedResponse[WebhookResponse],
    summary="List webhook endpoints",
    description=(
        "Lists your endpoints with their delivery health. A rising `failureCount` and a "
        "`lastError` mean your receiver is rejecting or timing out."
    ),
    responses={200: {"description": "A page of endpoints."}, 401: UNAUTHORIZED},
)
async def list_webhooks(
    service: ServiceDep, page: PageParamsDep
) -> PaginatedResponse[WebhookResponse]:
    result = await service.list_endpoints(page)
    return PaginatedResponse.of(
        items=[_webhook(endpoint) for endpoint in result.items],
        page=result.page,
        page_size=result.page_size,
        total_items=result.total,
    )


@router.patch(
    "/webhooks/{webhook_id}",
    response_model=ApiResponse[WebhookResponse],
    summary="Update a webhook endpoint",
    description=(
        "Changes the URL, the events, or the status. Set `status` to `disabled` to stop deliveries "
        "while keeping the endpoint and its secret."
    ),
    responses={
        200: {"description": "The updated endpoint."},
        401: UNAUTHORIZED,
        404: WEBHOOK_NOT_FOUND,
        422: {"description": "An unknown event (`UNKNOWN_WEBHOOK_EVENT`)."},
    },
)
async def update_webhook(
    webhook_id: WebhookIdPath, payload: UpdateWebhookRequest, service: ServiceDep
) -> ApiResponse[WebhookResponse]:
    endpoint = await service.update_endpoint(
        webhook_id,
        url=payload.url,
        events=[event.value for event in payload.events] if payload.events else None,
        status=payload.status,
    )
    return ApiResponse.ok(_webhook(endpoint))


@router.post(
    "/webhooks/{webhook_id}/test",
    response_model=ApiResponse[WebhookTestResponse],
    status_code=200,
    summary="Send a test delivery",
    description=(
        "Sends a signed `webhook.test` event and waits for the result, so you can confirm your "
        "receiver is reachable and your signature check works before relying on real events. "
        "The outcome is recorded on the endpoint either way."
    ),
    responses={
        200: {"description": "The test was attempted; `delivered` says whether it succeeded."},
        401: UNAUTHORIZED,
        404: WEBHOOK_NOT_FOUND,
    },
)
async def test_webhook(
    webhook_id: WebhookIdPath, service: ServiceDep
) -> ApiResponse[WebhookTestResponse]:
    delivered, error = await service.test_endpoint(webhook_id)
    return ApiResponse.ok(WebhookTestResponse(delivered=delivered, error=error))


@router.delete(
    "/webhooks/{webhook_id}",
    response_model=ApiResponse[None],
    summary="Delete a webhook endpoint",
    description="Removes the endpoint and its secret permanently. Deliveries stop immediately.",
    responses={
        200: {"description": "The endpoint was deleted."},
        401: UNAUTHORIZED,
        404: WEBHOOK_NOT_FOUND,
    },
)
async def delete_webhook(webhook_id: WebhookIdPath, service: ServiceDep) -> ApiResponse[None]:
    await service.delete_endpoint(webhook_id)
    return ApiResponse.ok(message="Webhook endpoint deleted.")
