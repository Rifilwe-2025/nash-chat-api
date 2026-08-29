"""WhatsApp message reads and the one unscoped lookup a public webhook needs.

Everything a signed-in tenant touches goes through :class:`WhatsAppMessageRepository`, which is
tenant-scoped like every other repository in the project.

:func:`resolve_connection` is the exception, and it is the same exception the public chat API makes
in ``api_keys/domain/repositories.py``: a webhook arrives with no token and no tenant, so something
has to *establish* the tenant before scoping is possible. It is a module-level function rather than
a method, so it cannot be reached through an object a request already holds, and it returns the row
rather than a decision — whether the signature is valid and the connection enabled is the service's
call. The lookup is by primary key, which a caller must already know: the id is a random UUID that
only appears in the webhook URL the tenant pastes into Meta.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.channels.domain.models import ChannelConfig, ChannelType
from src.modules.channels.whatsapp.domain.models import (
    DeliveryStatus,
    MessageDirection,
    WhatsAppMessage,
)
from src.shared.database.pagination import Page, PageRequest
from src.shared.database.repository import TenantScopedRepository


async def resolve_connection(
    session: AsyncSession, connection_id: uuid.UUID
) -> ChannelConfig | None:
    """Find a WhatsApp connection by id, across every tenant.

    The webhook's equivalent of authenticating an API key. Constrained to ``channel_type =
    'whatsapp'`` so a web channel's id cannot be used to reach this path at all.
    """
    query = select(ChannelConfig).where(
        ChannelConfig.id == connection_id,
        ChannelConfig.channel_type == ChannelType.WHATSAPP,
    )
    return (await session.execute(query)).scalar_one_or_none()


async def find_by_provider_message_id(
    session: AsyncSession, connection_id: uuid.UUID, provider_message_id: str
) -> WhatsAppMessage | None:
    """Look up one message by the provider's id, for a webhook that has no tenant yet.

    Used by delivery receipts, which name a message we sent and nothing else. Scoped to the
    connection the delivery arrived on, so a receipt can only ever touch that number's own messages.
    """
    query = select(WhatsAppMessage).where(
        WhatsAppMessage.connection_id == connection_id,
        WhatsAppMessage.provider_message_id == provider_message_id,
    )
    return (await session.execute(query)).scalar_one_or_none()


class WhatsAppMessageRepository(TenantScopedRepository[WhatsAppMessage]):
    model = WhatsAppMessage

    async def last_inbound_at(
        self, connection_id: uuid.UUID, wa_contact_id: str
    ) -> datetime | None:
        """When this contact last wrote — the moment the 24-hour window opened.

        Served by ``ix_whatsapp_message_contact``, which is ordered to match this exactly.
        """
        query = (
            select(WhatsAppMessage.created_at)
            .where(
                WhatsAppMessage.connection_id == connection_id,
                WhatsAppMessage.wa_contact_id == wa_contact_id,
                WhatsAppMessage.direction == MessageDirection.INBOUND,
            )
            .order_by(WhatsAppMessage.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def list_for_connection(
        self,
        connection_id: uuid.UUID,
        page: PageRequest,
        direction: MessageDirection | None = None,
        status: DeliveryStatus | None = None,
        wa_contact_id: str | None = None,
    ) -> Page[WhatsAppMessage]:
        query = self._base_query().where(WhatsAppMessage.connection_id == connection_id)
        if direction is not None:
            query = query.where(WhatsAppMessage.direction == direction)
        if status is not None:
            query = query.where(WhatsAppMessage.status == status)
        if wa_contact_id is not None:
            query = query.where(WhatsAppMessage.wa_contact_id == wa_contact_id)

        total = (
            await self.session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = await self.session.execute(
            query.order_by(WhatsAppMessage.created_at.desc()).offset(page.offset).limit(page.limit)
        )
        return Page(
            items=list(rows.scalars().all()),
            total=total,
            page=page.page,
            page_size=page.page_size,
        )
