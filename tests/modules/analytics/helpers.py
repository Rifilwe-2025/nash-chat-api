"""Seeding for the analytics tests.

Analytics is a read model, so what these tests need is *rows*, not turns. Driving a real
conversation would mean a fake provider, retrieval, guardrails and a lock per message — every one of
which has its own tests already — to produce message rows a test could write directly and control
precisely. Writing them here is what makes it possible to place traffic on a specific day, give one
reply a known token count, and assert that the totals come back exactly.

The rows are the real models, written through the real session, so the queries under test run
against the schema they will run against in production.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.agents.domain.models import Agent, AgentStatus, ModelProvider
from src.modules.conversations.domain.models import (
    Channel,
    Conversation,
    ConversationStatus,
    Message,
    MessageRole,
)

MODEL = "gemini-2.0-flash"
PROVIDER = "gemini"


@dataclass(frozen=True, slots=True)
class Turn:
    """One exchange: what was asked, what was answered, and what it cost."""

    question: str = "Do you deliver to Bulawayo?"
    answer: str = "Yes, within two working days."
    prompt_tokens: int = 100
    completion_tokens: int = 20
    cost_micro_usd: int | None = 1_500
    has_context: bool = True
    guardrail: str | None = None
    citations: list[dict[str, str]] | None = None


async def make_agent(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str = "Support",
    status: AgentStatus = AgentStatus.PUBLISHED,
) -> Agent:
    agent = Agent(
        tenant_id=tenant_id,
        name=name,
        persona="You are the support assistant for Nash Paints.",
        model_provider=ModelProvider.GEMINI,
        model_config_json={"model": MODEL},
        status=status,
    )
    session.add(agent)
    await session.flush()
    return agent


async def seed_conversation(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    turns: list[Turn],
    channel: Channel = Channel.WEB,
    at: datetime | None = None,
    escalated: bool = False,
    status: ConversationStatus = ConversationStatus.ACTIVE,
) -> Conversation:
    """One conversation with its messages, all stamped at ``at``.

    ``created_at`` is set explicitly rather than left to the server default: every figure analytics
    returns is bounded by a window, and a test that cannot place a message on a particular day
    cannot check that the window is being applied at all.
    """
    moment = at or datetime.now(UTC)
    conversation = Conversation(
        tenant_id=tenant_id,
        agent_id=agent_id,
        channel=channel,
        external_user_id=f"user-{uuid.uuid4().hex[:8]}",
        status=ConversationStatus.ESCALATED if escalated else status,
        created_at=moment,
        last_message_at=moment,
        escalated_at=moment if escalated else None,
        escalation_reason="Customer asked for a person." if escalated else None,
    )
    session.add(conversation)
    await session.flush()

    sequence = 0
    for turn in turns:
        sequence += 1
        session.add(
            Message(
                conversation_id=conversation.id,
                sequence=sequence,
                role=MessageRole.USER,
                content=turn.question,
                created_at=moment,
            )
        )
        sequence += 1
        session.add(
            Message(
                conversation_id=conversation.id,
                sequence=sequence,
                role=MessageRole.ASSISTANT,
                content=turn.answer,
                provider=PROVIDER,
                model=MODEL,
                prompt_tokens=turn.prompt_tokens,
                completion_tokens=turn.completion_tokens,
                cost_micro_usd=turn.cost_micro_usd,
                citations_json=turn.citations or [],
                meta_json=_meta(turn),
                created_at=moment,
            )
        )

    await session.flush()
    return conversation


def _meta(turn: Turn) -> dict[str, object]:
    """The markers the conversation engine writes, which the quality signals are measured from."""
    if turn.guardrail is not None:
        return {"guardrail": turn.guardrail, "matched": "refund"}
    return {"tier": "keyword", "hasContext": turn.has_context, "historyTurns": 0}


def days_ago(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)
