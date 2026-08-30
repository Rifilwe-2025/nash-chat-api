"""Plan limits and usage metering (spec §5.9).

The module has two jobs and they pull in opposite directions, so both are written with the other in
mind.

**Enforcement runs on the request path**, before an agent is created, before a turn is answered,
before an upload is stored. It has to be cheap — one indexed read against a counter or a count of
rows — and it has to fail in a way a caller can act on: a 402 naming which limit, what is used, and
what is allowed. A quota refusal that says only "no" is a support ticket.

**Metering runs after the work**, adding to the period's counters. It must never be the reason a
turn fails: a customer who got their answer has been served, and losing a metering write is a
smaller wrong than losing the answer. So :meth:`meter` swallows its own failures and says so in the
log.

**Why other modules call in rather than being read from.** Agents, conversations and the knowledge
base each ask this service before doing the thing that consumes quota. Billing does not observe them
from outside — there is no hook, no event bus, and no wrapper — because the check has to be *before*
the write and in the same transaction as it. The dependency runs one way: those modules import this
one, and this one reads their models through tenant-scoped repositories rather than their services
(see ``repositories.py`` for why that direction is the one that avoids a cycle).

Nothing here charges anybody. Pricing and the payment provider are §9's open question; this counts
what was used and enforces the ceilings a plan advertises.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src import configs
from src.modules.billing.domain.models import UsageMetric
from src.modules.billing.domain.repositories import (
    AgentQuotaRepository,
    StorageQuotaRepository,
    UsageCounterRepository,
)
from src.modules.billing.internal import plans
from src.modules.billing.internal.plans import UNLIMITED, Plan
from src.modules.tenants.domain.services import TenantService
from src.shared.exceptions import PlanLimitException

logger = logging.getLogger("api.billing")


def current_period(moment: datetime | None = None) -> str:
    """The billing period a moment falls in, as ``YYYY-MM`` in UTC.

    UTC rather than a tenant's local time: a period boundary that moved with the reader would let
    the same message land in two different months depending on who asked.
    """
    return (moment or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m")


@dataclass(frozen=True, slots=True)
class LimitUsage:
    """One limit: what is allowed, what is used, and therefore what is left."""

    limit: int
    used: int

    @property
    def unlimited(self) -> bool:
        return self.limit == UNLIMITED

    @property
    def remaining(self) -> int | None:
        """``None`` when unlimited — a number here would be a ceiling that does not exist."""
        return None if self.unlimited else max(self.limit - self.used, 0)

    @property
    def exceeded(self) -> bool:
        return not self.unlimited and self.used >= self.limit


@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    """Everything a tenant needs to answer "am I close to a limit?" in one response."""

    plan: Plan
    period: str
    enforced: bool
    agents: LimitUsage
    messages: LimitUsage
    storage: LimitUsage
    prompt_tokens: int
    completion_tokens: int
    cost_micro_usd: int


class BillingService:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.counters = UsageCounterRepository(session, tenant_id)
        self.agent_counts = AgentQuotaRepository(session, tenant_id)
        self.storage = StorageQuotaRepository(session, tenant_id)
        # Service to service for the tenant row itself: the plan a tenant is on is the tenants
        # module's fact, and this module only decides what that plan allows.
        self.tenants = TenantService(session)

    # -- reads ---------------------------------------------------------------

    async def plan(self) -> Plan:
        tenant = await self.tenants.get_tenant(self.tenant_id)
        return plans.for_tenant(tenant.plan.value)

    async def snapshot(self, period: str | None = None) -> PlanSnapshot:
        """The plan, its ceilings, and this period's usage against them."""
        billing_period = period or current_period()
        plan = await self.plan()
        counters = await self.counters.for_period(billing_period)

        return PlanSnapshot(
            plan=plan,
            period=billing_period,
            enforced=plans.enforced(),
            agents=LimitUsage(limit=plan.agents, used=await self.agent_counts.count()),
            messages=LimitUsage(
                limit=plan.messages_per_month, used=counters.get(UsageMetric.MESSAGES, 0)
            ),
            storage=LimitUsage(limit=plan.storage_bytes, used=await self.storage.bytes_used()),
            prompt_tokens=counters.get(UsageMetric.PROMPT_TOKENS, 0),
            completion_tokens=counters.get(UsageMetric.COMPLETION_TOKENS, 0),
            cost_micro_usd=counters.get(UsageMetric.COST_MICRO_USD, 0),
        )

    async def history(self, periods: int | None = None) -> dict[str, dict[UsageMetric, int]]:
        limit: int = periods or configs.BILLING_HISTORY_PERIODS
        return await self.counters.history(limit)

    # -- enforcement ---------------------------------------------------------

    async def check_agent_quota(self) -> None:
        """Called before an agent is created."""
        if not plans.enforced():
            return
        plan = await self.plan()
        used = await self.agent_counts.count()
        if not plan.allows(plan.agents, used):
            raise self._refusal("agents", plan.name, plan.agents, used)

    async def check_message_quota(self) -> None:
        """Called before a turn is answered, on every channel.

        Checked *before* the model call rather than after, because the point of a message quota is
        to bound what the platform spends on a tenant's behalf — a limit enforced after the provider
        has already been paid is an accounting note, not a limit.
        """
        if not plans.enforced():
            return
        plan = await self.plan()
        used = await self.counters.value(current_period(), UsageMetric.MESSAGES)
        if not plan.allows(plan.messages_per_month, used):
            raise self._refusal(
                "monthly messages", plan.name, plan.messages_per_month, used, resets=True
            )

    async def check_storage_quota(self, additional_bytes: int) -> None:
        """Called before an upload is stored, with the size of what is about to be added."""
        if not plans.enforced():
            return
        plan = await self.plan()
        used = await self.storage.bytes_used()
        if not plan.allows(plan.storage_bytes, used, adding=additional_bytes):
            raise self._refusal(
                "stored knowledge (bytes)",
                plan.name,
                plan.storage_bytes,
                used,
                adding=additional_bytes,
            )

    # -- metering ------------------------------------------------------------

    async def meter(
        self,
        messages: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_micro_usd: int = 0,
    ) -> None:
        """Add this turn's usage to the period's counters.

        Never raises. It is called after the work has succeeded, and a failed counter write must not
        turn a delivered answer into an error the customer sees. The loss is one row of metering,
        which is recoverable from the message rows if it ever has to be; the alternative loses the
        answer, which is not.
        """
        try:
            period = current_period()
            for metric, amount in (
                (UsageMetric.MESSAGES, messages),
                (UsageMetric.PROMPT_TOKENS, prompt_tokens),
                (UsageMetric.COMPLETION_TOKENS, completion_tokens),
                (UsageMetric.COST_MICRO_USD, cost_micro_usd),
            ):
                await self.counters.increment(period, metric, amount)
        except Exception:
            logger.exception("could not meter usage for tenant %s", self.tenant_id)

    # -- internals -----------------------------------------------------------

    def _refusal(
        self,
        what: str,
        plan_name: str,
        limit: int,
        used: int,
        adding: int = 1,
        resets: bool = False,
    ) -> PlanLimitException:
        """A refusal that names the limit, the usage, and what to do about it.

        Everything a caller needs to act is in the detail: which ceiling, what their plan allows,
        what they have used, and — for the monthly one — that it resets rather than requiring an
        upgrade. A 402 that says only "limit reached" is a support ticket.
        """
        when = " It resets at the start of the next month." if resets else ""
        return PlanLimitException(
            f"Your {plan_name} plan allows {limit} {what}; {used} in use"
            f"{f' and {adding} requested' if adding > 1 else ''}.{when}",
            code="PLAN_LIMIT_EXCEEDED",
        )
