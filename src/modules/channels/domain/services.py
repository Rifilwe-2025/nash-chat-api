"""Channel configuration, webhook management, and the channel-agnostic turn (spec §5.5, §5.6).

``ChannelService.handle`` is the seam every channel adapter goes through. It takes an
:class:`IncomingMessage`, runs the turn via ``ConversationService``, emits the platform events, and
returns an :class:`OutgoingMessage`. The web adapter in ``channels/web`` is the only caller today;
WhatsApp in Phase 10 is the second, and nothing between here and the model changes when it lands.

Webhook delivery is fired without being awaited by the caller. A tenant's slow receiver must not
make a customer wait, and a broken one must not fail their message — failures are recorded on the
endpoint instead. Durable retry arrives with the queue in Phase 9.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.agents.domain.services import AgentService
from src.modules.channels.domain.messages import IncomingMessage, OutgoingMessage
from src.modules.channels.domain.models import (
    ChannelConfig,
    ChannelStatus,
    ChannelType,
    WebhookEndpoint,
    WebhookEvent,
    WebhookStatus,
)
from src.modules.channels.domain.repositories import (
    ChannelConfigRepository,
    WebhookEndpointRepository,
)
from src.modules.channels.internal import webhooks
from src.modules.conversations.domain.models import Channel
from src.modules.conversations.domain.services import ConversationService, TurnResult
from src.shared.database.pagination import Page, PageRequest
from src.shared.exceptions import ConflictException, NotFoundException, ValidationException

logger = logging.getLogger("api.channels")

CHANNEL_MAP: dict[str, Channel] = {
    ChannelType.WEB.value: Channel.WEB,
    ChannelType.WHATSAPP.value: Channel.WHATSAPP,
}


class ChannelService:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.configs = ChannelConfigRepository(session, tenant_id)
        self.endpoints = WebhookEndpointRepository(session, tenant_id)
        self.agents = AgentService(session, tenant_id)

    # -- the channel-agnostic turn -------------------------------------------

    async def handle(
        self, incoming: IncomingMessage, conversations: ConversationService
    ) -> OutgoingMessage:
        """Run one inbound message through the engine and emit the resulting events."""
        existing = await conversations.conversations.find_open_session(
            incoming.agent_id, CHANNEL_MAP[incoming.channel], incoming.external_user_id
        )

        result = await conversations.send_message(
            agent_id=incoming.agent_id,
            content=incoming.text,
            channel=CHANNEL_MAP[incoming.channel],
            external_user_id=incoming.external_user_id,
        )

        if existing is None:
            await self.emit(WebhookEvent.CONVERSATION_STARTED, result)
        if result.escalated:
            await self.emit(WebhookEvent.CONVERSATION_ESCALATED, result)

        return OutgoingMessage(
            conversation_id=result.conversation.id,
            text=result.reply.content,
            escalated=result.escalated,
            citations=list(result.reply.citations_json),
        )

    # -- webhook emission ----------------------------------------------------

    async def emit(self, event: WebhookEvent, result: TurnResult) -> None:
        """Deliver an event to every endpoint subscribed to it, without blocking the turn."""
        endpoints = [
            endpoint
            for endpoint in await self.endpoints.active_for_agent(result.conversation.agent_id)
            if endpoint.subscribes_to(event)
        ]
        if not endpoints:
            return

        payload = webhooks.build_payload(
            event.value,
            {
                "conversationId": str(result.conversation.id),
                "agentId": str(result.conversation.agent_id),
                "tenantId": str(self.tenant_id),
                "channel": result.conversation.channel.value,
                "externalUserId": result.conversation.external_user_id,
                "status": result.conversation.status.value,
                "escalationReason": result.conversation.escalation_reason,
            },
        )

        for endpoint in endpoints:
            # Fire and forget: the reference is dropped deliberately, and `deliver` never raises.
            task = asyncio.create_task(self._deliver(endpoint.url, endpoint.secret, payload))
            _BACKGROUND.add(task)
            task.add_done_callback(_BACKGROUND.discard)

    async def _deliver(self, url: str, secret: str, payload: str) -> None:
        delivered, error = await webhooks.deliver(url, secret, payload)
        if not delivered:
            logger.warning("webhook delivery to %s failed: %s", url, error)

    async def test_endpoint(self, endpoint_id: uuid.UUID) -> tuple[bool, str | None]:
        """Send a signed ping, awaited, so a tenant can check their receiver from the console."""
        endpoint = await self.get_endpoint(endpoint_id)
        payload = webhooks.build_payload("webhook.test", {"endpointId": str(endpoint.id)})
        delivered, error = await webhooks.deliver(endpoint.url, endpoint.secret, payload)

        await self.endpoints.update(
            endpoint,
            last_delivery_at=datetime.now(UTC),
            failure_count=0 if delivered else endpoint.failure_count + 1,
            last_error=None if delivered else (error or "")[:500],
        )
        return delivered, error

    # -- webhook management --------------------------------------------------

    async def get_endpoint(self, endpoint_id: uuid.UUID) -> WebhookEndpoint:
        endpoint = await self.endpoints.get(endpoint_id)
        if endpoint is None:
            raise NotFoundException("Webhook endpoint does not exist.", code="WEBHOOK_NOT_FOUND")
        return endpoint

    async def list_endpoints(self, page: PageRequest) -> Page[WebhookEndpoint]:
        return await self.endpoints.list_endpoints(page)

    async def create_endpoint(
        self,
        url: str,
        events: list[str],
        agent_id: uuid.UUID | None = None,
    ) -> WebhookEndpoint:
        if agent_id is not None:
            await self.agents.get(agent_id)

        return await self.endpoints.add(
            WebhookEndpoint(
                agent_id=agent_id,
                url=url,
                secret=webhooks.generate_secret(),
                events=self._validate_events(events),
                status=WebhookStatus.ACTIVE,
            )
        )

    async def update_endpoint(
        self,
        endpoint_id: uuid.UUID,
        url: str | None = None,
        events: list[str] | None = None,
        status: WebhookStatus | None = None,
    ) -> WebhookEndpoint:
        endpoint = await self.get_endpoint(endpoint_id)

        changes: dict[str, object] = {}
        if url is not None:
            changes["url"] = url
        if events is not None:
            changes["events"] = self._validate_events(events)
        if status is not None:
            changes["status"] = status

        if not changes:
            return endpoint
        return await self.endpoints.update(endpoint, **changes)

    async def delete_endpoint(self, endpoint_id: uuid.UUID) -> None:
        await self.endpoints.delete(await self.get_endpoint(endpoint_id))

    # -- channel configuration -----------------------------------------------

    async def configure(
        self,
        agent_id: uuid.UUID,
        channel_type: ChannelType,
        settings: dict[str, object] | None = None,
        credentials: dict[str, object] | None = None,
    ) -> ChannelConfig:
        """Create or update an agent's settings for one channel."""
        agent = await self.agents.get(agent_id)
        existing = await self.configs.for_agent(agent.id, channel_type)

        if existing is not None:
            return await self.configs.update(
                existing,
                settings_json=settings if settings is not None else existing.settings_json,
                credentials_json=(
                    credentials if credentials is not None else existing.credentials_json
                ),
            )

        return await self.configs.add(
            ChannelConfig(
                agent_id=agent.id,
                channel_type=channel_type,
                status=ChannelStatus.ACTIVE,
                settings_json=settings or {},
                credentials_json=credentials or {},
            )
        )

    async def get_config(self, agent_id: uuid.UUID, channel_type: ChannelType) -> ChannelConfig:
        await self.agents.get(agent_id)
        config = await self.configs.for_agent(agent_id, channel_type)
        if config is None:
            raise NotFoundException(
                f"This agent has no {channel_type.value} channel configured.",
                code="CHANNEL_NOT_CONFIGURED",
            )
        return config

    async def list_configs(self, agent_id: uuid.UUID) -> list[ChannelConfig]:
        await self.agents.get(agent_id)
        return await self.configs.list_for_agent(agent_id)

    async def assert_channel_enabled(self, agent_id: uuid.UUID, channel_type: ChannelType) -> None:
        """A channel with no configuration is open; one explicitly disabled is closed.

        Requiring configuration before an agent could answer would mean a published agent that
        silently does nothing, which is a worse default than working out of the box.
        """
        config = await self.configs.for_agent(agent_id, channel_type)
        if config is not None and config.status is ChannelStatus.DISABLED:
            raise ConflictException(
                f"The {channel_type.value} channel is disabled for this agent.",
                code="CHANNEL_DISABLED",
            )

    def _validate_events(self, events: list[str]) -> list[str]:
        known = {event.value for event in WebhookEvent}
        unknown = [event for event in events if event not in known]
        if unknown:
            raise ValidationException(
                f"Unknown events: {', '.join(sorted(unknown))}. Supported: "
                f"{', '.join(sorted(known))}.",
                code="UNKNOWN_WEBHOOK_EVENT",
            )
        if not events:
            raise ValidationException(
                "Subscribe to at least one event.", code="WEBHOOK_NEEDS_EVENT"
            )
        return sorted(set(events))


# Strong references to in-flight deliveries. Without this the event loop may garbage-collect a
# task that nothing awaits, and the delivery silently never happens.
_BACKGROUND: set[asyncio.Task[None]] = set()
