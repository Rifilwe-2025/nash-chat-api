"""Enum columns store the member's value, and the database defaults agree with them.

Regression test for a defect found while building tier routing. SQLAlchemy's default is to persist
an enum's ``.name`` (``"DRAFT"``), while every ``server_default`` in the migrations — and every JSON
response — uses its ``.value`` (``"draft"``). The two disagreed silently, because the ORM always
supplies these columns on insert; only a row written without one would hit the default and then fail
to load.

Both halves are asserted here: what the ORM writes, and what happens to a row that leans on the
database default instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.agents.domain.models import Agent, AgentStatus, ModelProvider
from src.modules.knowledge_base.domain.models import (
    KbSource,
    KnowledgeBase,
    RetrievalTier,
    SourceStatus,
    SourceType,
)
from src.modules.tenants.domain.models import Tenant


@pytest.fixture
async def tenant(make_tenant: Callable[..., Coroutine[Any, Any, Tenant]]) -> Tenant:
    return await make_tenant(name="Nash Paints")


async def stored_value(session: AsyncSession, table: str, column: str, row_id: uuid.UUID) -> str:
    result = await session.execute(
        text(f'SELECT {column} FROM "{table}" WHERE id = :id'), {"id": row_id}
    )
    return str(result.scalar_one())


async def test_the_orm_writes_the_lowercase_value(session: AsyncSession, tenant: Tenant) -> None:
    agent = Agent(
        tenant_id=tenant.id,
        name="Sales Assistant",
        status=AgentStatus.PUBLISHED,
        model_provider=ModelProvider.GEMINI,
    )
    session.add(agent)
    await session.flush()

    assert await stored_value(session, "agent", "status", agent.id) == "published"
    assert await stored_value(session, "agent", "model_provider", agent.id) == "gemini"


async def test_the_knowledge_base_enums_store_their_values(
    session: AsyncSession, tenant: Tenant
) -> None:
    knowledge_base = KnowledgeBase(
        tenant_id=tenant.id, name="Policies", retrieval_tier=RetrievalTier.KEYWORD
    )
    session.add(knowledge_base)
    await session.flush()

    source = KbSource(
        tenant_id=tenant.id,
        kb_id=knowledge_base.id,
        name="Returns",
        type=SourceType.MANUAL,
        status=SourceStatus.READY,
        extracted_text="Paint may be returned within 30 days.",
    )
    session.add(source)
    await session.flush()

    assert await stored_value(session, "knowledge_base", "retrieval_tier", knowledge_base.id) == (
        "keyword"
    )
    assert await stored_value(session, "kb_source", "type", source.id) == "manual"
    assert await stored_value(session, "kb_source", "status", source.id) == "ready"


async def test_a_row_that_leans_on_the_database_default_loads_back(
    session: AsyncSession, tenant: Tenant
) -> None:
    """The case the mismatch would have broken: an insert that omits the column entirely.

    Written as raw SQL precisely because the ORM would otherwise supply the value and hide the
    problem — a migration backfill or a psql session inserts exactly like this.
    """
    kb_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO knowledge_base (id, tenant_id, name) VALUES (:id, :tenant_id, :name)"),
        {"id": kb_id, "tenant_id": tenant.id, "name": "Defaulted"},
    )

    loaded = (
        await session.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    ).scalar_one()

    assert loaded.retrieval_tier is RetrievalTier.AUTO


async def test_the_default_source_status_also_round_trips(
    session: AsyncSession, tenant: Tenant
) -> None:
    knowledge_base = KnowledgeBase(tenant_id=tenant.id, name="Policies")
    session.add(knowledge_base)
    await session.flush()

    source_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO kb_source (id, tenant_id, kb_id, name, type) "
            "VALUES (:id, :tenant_id, :kb_id, :name, 'manual')"
        ),
        {"id": source_id, "tenant_id": tenant.id, "kb_id": knowledge_base.id, "name": "Typed in"},
    )

    loaded = (await session.execute(select(KbSource).where(KbSource.id == source_id))).scalar_one()

    assert loaded.status is SourceStatus.PENDING
    assert loaded.type is SourceType.MANUAL
