"""Generic repository base.

Repositories own every ``select(...)``. They **flush** so generated values (ids, defaults) are
available to the calling service, but they never **commit** — the session dependency in
:mod:`src.shared.database.dependencies` owns the transaction boundary.

Phase 2 adds the tenant-scoped subclass that makes cross-tenant reads impossible by construction.
"""

from __future__ import annotations

import uuid
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.database.base_model import BaseModel
from src.shared.database.pagination import Page, PageRequest

ModelT = TypeVar("ModelT", bound=BaseModel)


class BaseRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- reads ---------------------------------------------------------------

    def _base_query(self) -> Select[tuple[ModelT]]:
        """Every read starts here so subclasses can narrow the scope in one place."""
        return select(self.model)

    async def get(self, entity_id: uuid.UUID) -> ModelT | None:
        query = self._base_query().where(self.model.id == entity_id)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def list(self, page: PageRequest) -> Page[ModelT]:
        query = self._base_query().order_by(self.model.created_at.desc())

        count_query = select(func.count()).select_from(query.subquery())
        total = (await self.session.execute(count_query)).scalar_one()

        rows = await self.session.execute(query.offset(page.offset).limit(page.limit))
        return Page(
            items=list(rows.scalars().all()),
            total=total,
            page=page.page,
            page_size=page.page_size,
        )

    async def exists(self, entity_id: uuid.UUID) -> bool:
        query = select(func.count()).select_from(
            self._base_query().where(self.model.id == entity_id).subquery()
        )
        return (await self.session.execute(query)).scalar_one() > 0

    # -- writes --------------------------------------------------------------

    async def add(self, entity: ModelT) -> ModelT:
        self.session.add(entity)
        await self.session.flush()
        await self.session.refresh(entity)
        return entity

    async def update(self, entity: ModelT, **changes: Any) -> ModelT:
        for field, value in changes.items():
            setattr(entity, field, value)
        await self.session.flush()
        await self.session.refresh(entity)
        return entity

    async def delete(self, entity: ModelT) -> None:
        await self.session.delete(entity)
        await self.session.flush()

    async def delete_by_id(self, entity_id: uuid.UUID) -> int:
        result = await self.session.execute(delete(self.model).where(self.model.id == entity_id))
        await self.session.flush()
        return result.rowcount or 0
