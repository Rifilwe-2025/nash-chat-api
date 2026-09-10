"""Conversation reads — every ``select(...)`` for this module lives here.

``ConversationRepository`` is tenant-scoped. ``MessageRepository`` is not, and does not need to be:
a message is only reachable through a ``conversation_id`` that was itself loaded through the scoped
repository.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from src.modules.conversations.domain.models import (
    Channel,
    Conversation,
    ConversationStatus,
    Message,
    MessageRole,
)
from src.shared.database.pagination import Page, PageRequest
from src.shared.database.repository import BaseRepository, TenantScopedRepository


class ConversationRepository(TenantScopedRepository[Conversation]):
    model = Conversation

    async def find_open_session(
        self, agent_id: uuid.UUID, channel: Channel, external_user_id: str
    ) -> Conversation | None:
        """The live session for a (agent, channel, user) key, if there is one.

        Only ``ACTIVE`` counts. A closed conversation is finished, and an escalated one belongs to
        a human now — continuing to append agent replies to either would be wrong.
        """
        query = (
            self._base_query()
            .where(
                Conversation.agent_id == agent_id,
                Conversation.channel == channel,
                Conversation.external_user_id == external_user_id,
                Conversation.status == ConversationStatus.ACTIVE,
            )
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(query)).scalar_one_or_none()

    async def open_session(
        self, agent_id: uuid.UUID, channel: Channel, external_user_id: str
    ) -> Conversation:
        """The live session for a key, opened if there is none — correctly when two race for it.

        Looking for a session and then adding one is two statements, and two first messages from
        the same visitor (a double-click, two tabs) can both find nothing and both add, leaving two
        open conversations that each get half the transcript. The partial unique index
        ``uq_conversation_open_session`` makes the second insert a conflict, and ``ON CONFLICT DO
        NOTHING`` turns that conflict into "join the one that was just opened": Postgres makes the
        second insert wait for the first transaction, so the read that follows sees its row.
        """
        existing = await self.find_open_session(agent_id, channel, external_user_id)
        if existing is not None:
            return existing

        await self.session.execute(
            insert(Conversation)
            .values(
                tenant_id=self.tenant_id,
                agent_id=agent_id,
                channel=channel,
                external_user_id=external_user_id,
                status=ConversationStatus.ACTIVE,
            )
            .on_conflict_do_nothing(
                index_elements=["agent_id", "channel", "external_user_id"],
                # A literal predicate, not a bound one: Postgres can only match a partial index to
                # an ON CONFLICT clause whose predicate it can prove at plan time.
                index_where=text("status = 'active'"),
            )
        )

        opened = await self.find_open_session(agent_id, channel, external_user_id)
        if opened is None:  # pragma: no cover - the insert or the conflicting row guarantees one
            raise RuntimeError("an open session was neither created nor found")
        return opened

    async def list_conversations(
        self,
        page: PageRequest,
        agent_id: uuid.UUID | None = None,
        status: ConversationStatus | None = None,
        channel: Channel | None = None,
    ) -> Page[Conversation]:
        query = self._base_query()
        if agent_id is not None:
            query = query.where(Conversation.agent_id == agent_id)
        if status is not None:
            query = query.where(Conversation.status == status)
        if channel is not None:
            query = query.where(Conversation.channel == channel)

        total = (
            await self.session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = await self.session.execute(
            query.order_by(Conversation.last_message_at.desc().nullslast())
            .offset(page.offset)
            .limit(page.limit)
        )
        return Page(
            items=list(rows.scalars().all()),
            total=total,
            page=page.page,
            page_size=page.page_size,
        )


class MessageRepository(BaseRepository[Message]):
    model = Message

    async def history(self, conversation_id: uuid.UUID) -> list[Message]:
        """Spoken turns, oldest first — what trimming and the prompt are built from.

        Summary rows are excluded: they are a record of what was folded away, not part of the
        dialogue, and replaying one as a turn would confuse the model about who said what.
        """
        query = (
            self._base_query()
            .where(
                Message.conversation_id == conversation_id,
                Message.role != MessageRole.SUMMARY,
            )
            .order_by(Message.sequence)
        )
        return list((await self.session.execute(query)).scalars().all())

    async def transcript(self, conversation_id: uuid.UUID, page: PageRequest) -> Page[Message]:
        """Everything in the conversation, including summary markers — the debugging view."""
        query = self._base_query().where(Message.conversation_id == conversation_id)

        total = (
            await self.session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = await self.session.execute(
            query.order_by(Message.sequence).offset(page.offset).limit(page.limit)
        )
        return Page(
            items=list(rows.scalars().all()),
            total=total,
            page=page.page,
            page_size=page.page_size,
        )

    async def next_sequence(self, conversation_id: uuid.UUID) -> int:
        """The next position in this conversation.

        Safe against races because a turn holds the conversation's advisory lock before it writes
        (see ``internal/locking.py``); the unique constraint on (conversation, sequence) is the
        backstop if that is ever missed.
        """
        query = select(func.coalesce(func.max(Message.sequence), 0)).where(
            Message.conversation_id == conversation_id
        )
        return int((await self.session.execute(query)).scalar_one()) + 1

    async def usage_totals(self, conversation_id: uuid.UUID) -> tuple[int, int, int]:
        """``(prompt tokens, completion tokens, micro-USD)`` across the conversation."""
        query = select(
            func.coalesce(func.sum(Message.prompt_tokens), 0),
            func.coalesce(func.sum(Message.completion_tokens), 0),
            func.coalesce(func.sum(Message.cost_micro_usd), 0),
        ).where(Message.conversation_id == conversation_id)
        row = (await self.session.execute(query)).one()
        return int(row[0]), int(row[1]), int(row[2])
