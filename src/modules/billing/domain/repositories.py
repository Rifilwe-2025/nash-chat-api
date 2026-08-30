"""Usage counters and the state reads a quota check needs — every ``select(...)`` for this module.

Two kinds of read live here, for the same reason they do in ``analytics``:

* ``UsageCounterRepository`` owns this module's own table.
* ``AgentQuotaRepository`` and ``StorageQuotaRepository`` count rows that belong to other modules.
  Billing needs "how many agents" and "how many bytes" on the request path, and asking those modules
  service-to-service would mean importing them — while they, in turn, have to call billing to be
  checked. Reading their *models* through a tenant-scoped repository breaks that cycle without
  weakening anything: the scoping base is the same one those modules use, so the isolation is
  inherited rather than re-implemented (spec §5.7).

Both counts are of **current state**, not of accumulated usage, which is why neither is a counter:
deleting an agent frees its slot and deleting a source frees its bytes.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from src.modules.agents.domain.models import Agent
from src.modules.billing.domain.models import UsageCounter, UsageMetric
from src.modules.knowledge_base.domain.models import KbSource
from src.shared.database.repository import TenantScopedRepository


class UsageCounterRepository(TenantScopedRepository[UsageCounter]):
    model = UsageCounter

    async def increment(self, period: str, metric: UsageMetric, amount: int) -> None:
        """Add to a counter, creating it if this is the period's first use.

        A single ``INSERT … ON CONFLICT DO UPDATE``, not read-then-write. Two turns finishing at the
        same moment would otherwise both read the same total and both write it back, and the tenant
        would be metered for one of them — a race that gets *more* likely exactly when a tenant is
        busy enough for the numbers to matter.

        The tenant comes from ``self.tenant_id``, never from a caller's argument, so a metering call
        cannot be pointed at somebody else's account.
        """
        if amount == 0:
            return

        statement = (
            insert(UsageCounter)
            .values(
                tenant_id=self.tenant_id,
                period=period,
                metric=metric,
                value=amount,
            )
            .on_conflict_do_update(
                constraint="uq_usage_counter_tenant_id_period_metric",
                set_={"value": UsageCounter.value + amount},
            )
        )
        await self.session.execute(statement)
        await self.session.flush()

    async def value(self, period: str, metric: UsageMetric) -> int:
        query = self._base_query().where(
            UsageCounter.period == period, UsageCounter.metric == metric
        )
        counter = (await self.session.execute(query)).scalar_one_or_none()
        return counter.value if counter else 0

    async def for_period(self, period: str) -> dict[UsageMetric, int]:
        query = self._base_query().where(UsageCounter.period == period)
        rows = (await self.session.execute(query)).scalars().all()
        return {counter.metric: counter.value for counter in rows}

    async def history(self, periods: int) -> dict[str, dict[UsageMetric, int]]:
        """The most recent periods, newest first.

        Ordered and limited by *period* rather than by row, so a month with four metrics does not
        crowd out the month before it.
        """
        recent = (
            select(UsageCounter.period)
            .where(UsageCounter.tenant_id == self.tenant_id)
            .group_by(UsageCounter.period)
            .order_by(UsageCounter.period.desc())
            .limit(periods)
            .subquery()
        )
        rows = await self.session.execute(
            self._base_query()
            .join(recent, UsageCounter.period == recent.c.period)
            .order_by(UsageCounter.period.desc())
        )

        grouped: dict[str, dict[UsageMetric, int]] = {}
        for counter in rows.scalars():
            grouped.setdefault(counter.period, {})[counter.metric] = counter.value
        return grouped


class AgentQuotaRepository(TenantScopedRepository[Agent]):
    """How many agents this tenant has right now."""

    model = Agent

    async def count(self) -> int:
        total = await self.session.execute(
            select(func.count()).select_from(self._base_query().subquery())
        )
        return int(total.scalar_one())


class StorageQuotaRepository(TenantScopedRepository[KbSource]):
    """How many bytes of knowledge this tenant is storing right now.

    ``byte_size`` is the size of the *submitted* content, which is what the knowledge base module
    already measures its own limits against — so the plan ceiling and the platform ceiling are
    counting the same thing, and a tenant near one is near the other.
    """

    model = KbSource

    async def bytes_used(self) -> int:
        # Summed over the scoped subquery's own column, not over `KbSource.byte_size`: referring to
        # the table from outside the subquery would join it back in and total every tenant's bytes.
        scoped = self._base_query().subquery()
        total = await self.session.execute(
            select(func.coalesce(func.sum(scoped.c.byte_size), 0)).select_from(scoped)
        )
        return int(total.scalar_one())
