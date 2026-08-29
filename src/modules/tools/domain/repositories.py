"""Tool, policy and call-log reads — every ``select(...)`` for this module lives here."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from src.modules.tools.domain.models import (
    AgentTool,
    ToolCallLog,
    ToolPolicy,
    ToolStatus,
)
from src.shared.database.pagination import Page, PageRequest
from src.shared.database.repository import BaseRepository, TenantScopedRepository


class AgentToolRepository(TenantScopedRepository[AgentTool]):
    model = AgentTool

    async def list_for_agent(self, agent_id: uuid.UUID, page: PageRequest) -> Page[AgentTool]:
        query = self._base_query().where(AgentTool.agent_id == agent_id)

        total = (
            await self.session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = await self.session.execute(
            query.order_by(AgentTool.name).offset(page.offset).limit(page.limit)
        )
        return Page(
            items=list(rows.scalars().all()),
            total=total,
            page=page.page,
            page_size=page.page_size,
        )

    async def enabled_for_agent(self, agent_id: uuid.UUID) -> list[AgentTool]:
        """The tools that go into a turn's prompt.

        Ordered by name so the tool list a model sees is stable between turns — an unstable ordering
        would defeat prompt caching and make a model's choices harder to reproduce when debugging.
        """
        query = (
            self._base_query()
            .where(AgentTool.agent_id == agent_id, AgentTool.status == ToolStatus.ENABLED)
            .order_by(AgentTool.name)
        )
        return list((await self.session.execute(query)).scalars().all())

    async def by_name(self, agent_id: uuid.UUID, name: str) -> AgentTool | None:
        query = self._base_query().where(AgentTool.agent_id == agent_id, AgentTool.name == name)
        return (await self.session.execute(query)).scalar_one_or_none()


class ToolPolicyRepository(TenantScopedRepository[ToolPolicy]):
    model = ToolPolicy

    async def for_agent(self, agent_id: uuid.UUID) -> ToolPolicy | None:
        query = self._base_query().where(ToolPolicy.agent_id == agent_id)
        return (await self.session.execute(query)).scalar_one_or_none()


class ToolCallLogRepository(BaseRepository[ToolCallLog]):
    """Not tenant-scoped: a call is only reached through its tool, which is.

    The same reasoning ``MessageRepository`` uses — and like that one, every read here takes the
    parent id the caller has already had scoped for it.
    """

    model = ToolCallLog

    async def list_for_tool(self, tool_id: uuid.UUID, page: PageRequest) -> Page[ToolCallLog]:
        query = self._base_query().where(ToolCallLog.tool_id == tool_id)

        total = (
            await self.session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = await self.session.execute(
            query.order_by(ToolCallLog.created_at.desc()).offset(page.offset).limit(page.limit)
        )
        return Page(
            items=list(rows.scalars().all()),
            total=total,
            page=page.page,
            page_size=page.page_size,
        )
