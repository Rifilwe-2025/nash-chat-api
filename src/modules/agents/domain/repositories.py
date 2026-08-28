"""Agent reads — every ``select(...)`` for this module lives here.

``AgentRepository`` is tenant-scoped, so it physically cannot return another tenant's agent.
``AgentVersionRepository`` is reached only with an ``agent_id`` that was itself loaded through the
scoped repository, which is what keeps version history isolated too.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from src.modules.agents.domain.models import Agent, AgentVersion
from src.shared.database.repository import BaseRepository, TenantScopedRepository


class AgentRepository(TenantScopedRepository[Agent]):
    model = Agent

    async def get_by_name(self, name: str) -> Agent | None:
        query = self._base_query().where(func.lower(Agent.name) == name.lower())
        return (await self.session.execute(query)).scalar_one_or_none()

    async def name_taken(self, name: str, exclude_id: uuid.UUID | None = None) -> bool:
        query = self._base_query().where(func.lower(Agent.name) == name.lower())
        if exclude_id is not None:
            query = query.where(Agent.id != exclude_id)

        count_query = select(func.count()).select_from(query.subquery())
        return (await self.session.execute(count_query)).scalar_one() > 0


class AgentVersionRepository(BaseRepository[AgentVersion]):
    model = AgentVersion

    async def list_for_agent(self, agent_id: uuid.UUID) -> list[AgentVersion]:
        query = (
            self._base_query()
            .where(AgentVersion.agent_id == agent_id)
            .order_by(AgentVersion.version.desc())
        )
        return list((await self.session.execute(query)).scalars().all())

    async def get_version(self, agent_id: uuid.UUID, version: int) -> AgentVersion | None:
        query = self._base_query().where(
            AgentVersion.agent_id == agent_id, AgentVersion.version == version
        )
        return (await self.session.execute(query)).scalar_one_or_none()
