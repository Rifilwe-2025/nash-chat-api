"""Resolving a WhatsApp request's tenant — from a token, or from the webhook URL.

Two ways in, and they are kept visibly apart because only one of them involves a user.

**The tenant-facing routes** take the tenant from the access token like every other module.

**The webhook** has no token: WhatsApp holds no credential of ours, and the delivery has to
establish its own tenant before anything can be scoped. That is what
:func:`get_webhook_connection` does — the same shape as the public chat API resolving a tenant from
an API key, and for the same reason. Every service built afterwards is scoped to the tenant *that
lookup returned*, never to anything the request body claimed, so a forged payload cannot reach
another tenant's data even if it guesses a connection id.

A disabled connection is refused here rather than deeper in: a tenant who switches the channel off
has said "stop", and the cheapest place to honour that is before any work begins.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Path

from src.modules.channels.domain.models import ChannelConfig, ChannelStatus
from src.modules.channels.whatsapp.domain.repositories import resolve_connection
from src.modules.channels.whatsapp.domain.services import WhatsAppService
from src.modules.conversations.domain.services import ConversationService
from src.modules.tenants.presentation.dependencies import CurrentTenantDep
from src.shared.database.dependencies import SessionDep
from src.shared.exceptions import ConflictException, NotFoundException


def get_whatsapp_service(session: SessionDep, tenant_id: CurrentTenantDep) -> WhatsAppService:
    """The tenant comes from the token, so every query below is scoped before it is written."""
    return WhatsAppService(session, tenant_id)


ServiceDep = Annotated[WhatsAppService, Depends(get_whatsapp_service)]


async def get_webhook_connection(
    session: SessionDep,
    connection_id: Annotated[uuid.UUID, Path()],
) -> ChannelConfig:
    """Resolve the connection a webhook delivery names.

    The one unscoped read this module makes, and the thing that establishes the tenant for
    everything that follows. A missing connection and a disabled one are distinguished because both
    are the tenant's own doing and neither leaks anything: the id came from a URL only they have.
    """
    connection = await resolve_connection(session, connection_id)
    if connection is None:
        raise NotFoundException(
            "No WhatsApp connection matches this webhook URL.", code="CHANNEL_NOT_CONFIGURED"
        )
    if connection.status is ChannelStatus.DISABLED:
        raise ConflictException(
            "The WhatsApp channel is disabled for this agent.", code="CHANNEL_DISABLED"
        )
    return connection


WebhookConnectionDep = Annotated[ChannelConfig, Depends(get_webhook_connection)]


@dataclass(frozen=True, slots=True)
class WebhookServices:
    """The services a webhook needs, all scoped to the tenant the connection resolved to.

    Bundled rather than injected one by one so the tenant is derived in exactly one place, and so a
    test can substitute the conversation engine's provider by overriding a single dependency.
    """

    whatsapp: WhatsAppService
    conversations: ConversationService


def get_webhook_services(session: SessionDep, connection: WebhookConnectionDep) -> WebhookServices:
    return WebhookServices(
        whatsapp=WhatsAppService(session, connection.tenant_id),
        conversations=ConversationService(session, connection.tenant_id),
    )


WebhookServicesDep = Annotated[WebhookServices, Depends(get_webhook_services)]
