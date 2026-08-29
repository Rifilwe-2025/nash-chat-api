"""Channel and webhook reads — every ``select(...)`` for this module lives here."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from src.modules.channels.domain.models import (
    ChannelConfig,
    ChannelType,
    WebhookEndpoint,
    WebhookStatus,
)
from src.shared.database.pagination import Page, PageRequest
from src.shared.database.repository import TenantScopedRepository


class ChannelConfigRepository(TenantScopedRepository[ChannelConfig]):
    model = ChannelConfig

    async def for_agent(
        self, agent_id: uuid.UUID, channel_type: ChannelType
    ) -> ChannelConfig | None:
        query = self._base_query().where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == channel_type,
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def list_for_agent(self, agent_id: uuid.UUID) -> list[ChannelConfig]:
        query = self._base_query().where(ChannelConfig.agent_id == agent_id)
        return list((await self.session.execute(query)).scalars().all())


class WebhookEndpointRepository(TenantScopedRepository[WebhookEndpoint]):
    model = WebhookEndpoint

    async def list_endpoints(self, page: PageRequest) -> Page[WebhookEndpoint]:
        query = self._base_query()

        total = (
            await self.session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = await self.session.execute(
            query.order_by(WebhookEndpoint.created_at.desc()).offset(page.offset).limit(page.limit)
        )
        return Page(
            items=list(rows.scalars().all()),
            total=total,
            page=page.page,
            page_size=page.page_size,
        )

    async def active_for_agent(self, agent_id: uuid.UUID) -> list[WebhookEndpoint]:
        """Endpoints that should receive events for this agent.

        An endpoint with no ``agent_id`` is tenant-wide and receives everything; one with an
        ``agent_id`` receives only that agent's events. Disabled endpoints are excluded here so no
        caller has to remember to check.
        """
        query = self._base_query().where(
            WebhookEndpoint.status == WebhookStatus.ACTIVE,
            (WebhookEndpoint.agent_id == agent_id) | (WebhookEndpoint.agent_id.is_(None)),
        )
        return list((await self.session.execute(query)).scalars().all())
