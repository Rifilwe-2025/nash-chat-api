"""One open session per visitor, however their first messages arrive (spec §5.4).

A double-clicked send button, or the same page open in two tabs, delivers two first messages at
once. Both used to look for an open conversation, both found none, and both opened one — splitting
the visitor's transcript across two threads. These tests pin the two halves of the fix: the database
refuses a second open session, and the losing side of a race joins the winner's instead of failing.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.conversations.domain.models import Channel, Conversation, ConversationStatus
from src.modules.conversations.domain.repositories import ConversationRepository
from src.modules.tenants.domain.models import Tenant
from tests.modules.conversations.test_turn import build_agent


class LateRepository(ConversationRepository):
    """Looks for a session before the other request's row exists — the losing side of the race."""

    looked = False

    async def find_open_session(
        self, agent_id: uuid.UUID, channel: Channel, external_user_id: str
    ) -> Conversation | None:
        if not self.looked:
            self.looked = True
            return None
        return await super().find_open_session(agent_id, channel, external_user_id)


@pytest.fixture
async def tenant(make_tenant: Callable[..., Coroutine[Any, Any, Tenant]]) -> Tenant:
    return await make_tenant(name="Nash Paints")


async def open_sessions(session: AsyncSession, agent_id: uuid.UUID) -> int:
    query = select(func.count()).where(
        Conversation.agent_id == agent_id, Conversation.status == ConversationStatus.ACTIVE
    )
    return int((await session.execute(query)).scalar_one())


async def test_the_database_refuses_a_second_open_session_for_one_visitor(
    session: AsyncSession, tenant: Tenant
) -> None:
    agent = await build_agent(session, tenant)
    repository = ConversationRepository(session, tenant.id)
    await repository.add(
        Conversation(
            agent_id=agent.id,
            channel=Channel.WEB,
            external_user_id="visitor-1",
            status=ConversationStatus.ACTIVE,
        )
    )

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                Conversation(
                    tenant_id=tenant.id,
                    agent_id=agent.id,
                    channel=Channel.WEB,
                    external_user_id="visitor-1",
                    status=ConversationStatus.ACTIVE,
                )
            )
            await session.flush()


async def test_the_losing_side_of_a_race_joins_the_session_the_winner_opened(
    session: AsyncSession, tenant: Tenant
) -> None:
    """Not an error and not a second conversation: the same one, as if it had looked later."""
    agent = await build_agent(session, tenant)
    winner = await ConversationRepository(session, tenant.id).add(
        Conversation(
            agent_id=agent.id,
            channel=Channel.WEB,
            external_user_id="visitor-1",
            status=ConversationStatus.ACTIVE,
        )
    )

    joined = await LateRepository(session, tenant.id).open_session(
        agent.id, Channel.WEB, "visitor-1"
    )

    assert joined.id == winner.id
    assert await open_sessions(session, agent.id) == 1


async def test_an_ended_conversation_does_not_stop_the_visitor_coming_back(
    session: AsyncSession, tenant: Tenant
) -> None:
    """Uniqueness is among *active* conversations only; history may repeat the key freely."""
    agent = await build_agent(session, tenant)
    repository = ConversationRepository(session, tenant.id)
    for status in (ConversationStatus.CLOSED, ConversationStatus.ESCALATED):
        await repository.add(
            Conversation(
                agent_id=agent.id,
                channel=Channel.WEB,
                external_user_id="visitor-1",
                status=status,
            )
        )

    opened = await repository.open_session(agent.id, Channel.WEB, "visitor-1")

    assert opened.status is ConversationStatus.ACTIVE
    assert await open_sessions(session, agent.id) == 1


async def test_the_same_visitor_on_another_channel_is_another_session(
    session: AsyncSession, tenant: Tenant
) -> None:
    agent = await build_agent(session, tenant)
    repository = ConversationRepository(session, tenant.id)

    web = await repository.open_session(agent.id, Channel.WEB, "visitor-1")
    preview = await repository.open_session(agent.id, Channel.PREVIEW, "visitor-1")

    assert web.id != preview.id
