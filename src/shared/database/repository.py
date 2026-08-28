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

from src.shared.database.base_model import BaseModel, TenantScopedModel
from src.shared.database.pagination import Page, PageRequest


class CrossTenantAccessError(RuntimeError):
    """Raised when a scoped repository is handed an entity from a different tenant.

    A programming error rather than a client error: reaching this means an object was loaded through
    an unscoped path. It is deliberately not an ``AppException`` — it should surface as a 500 and be
    fixed, not returned to a caller as a routine failure.
    """


ModelT = TypeVar("ModelT", bound=BaseModel)
TenantModelT = TypeVar("TenantModelT", bound=TenantScopedModel)


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


class TenantScopedRepository(BaseRepository[TenantModelT]):
    """Repository that cannot see outside its tenant.

    The scope is applied in ``_base_query``, which every read goes through, and re-applied on the
    bulk delete that bypasses it. Constructing one requires a ``tenant_id``, so there is no way to
    ask for "all rows" by forgetting a filter — the isolation is enforced by the query layer rather
    than by each endpoint remembering (spec §5.7).
    """

    model: type[TenantModelT]

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        super().__init__(session)
        self.tenant_id = tenant_id

    def _base_query(self) -> Select[tuple[TenantModelT]]:
        return select(self.model).where(self.model.tenant_id == self.tenant_id)

    async def add(self, entity: TenantModelT) -> TenantModelT:
        """Stamp the tenant on insert so a caller cannot write into another tenant."""
        entity.tenant_id = self.tenant_id
        return await super().add(entity)

    async def update(self, entity: TenantModelT, **changes: Any) -> TenantModelT:
        self._assert_owned(entity)
        changes.pop("tenant_id", None)
        return await super().update(entity, **changes)

    async def delete(self, entity: TenantModelT) -> None:
        self._assert_owned(entity)
        await super().delete(entity)

    async def delete_by_id(self, entity_id: uuid.UUID) -> int:
        result = await self.session.execute(
            delete(self.model).where(
                self.model.id == entity_id,
                self.model.tenant_id == self.tenant_id,
            )
        )
        await self.session.flush()
        return result.rowcount or 0

    def _assert_owned(self, entity: TenantModelT) -> None:
        if entity.tenant_id != self.tenant_id:
            raise CrossTenantAccessError(
                f"{type(entity).__name__} {entity.id} belongs to another tenant"
            )
