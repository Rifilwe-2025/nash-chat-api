"""Plan definitions, read from configuration (spec §5.9).

**Plans live in configuration, not in a table.** A plan is not tenant data — it is a product
decision that changes for everybody at once, and a table would mean a migration to change a number
and a per-tenant row that could silently drift from what the pricing page says. The tenant's *plan*
is a column on ``tenant`` (Phase 1); what that plan allows is here.

The format is one plan per entry, semicolons between them::

    free=agents:1,messages:500,storage:52428800;starter=agents:5,messages:10000,storage:524288000

Every limit is a number, and **-1 means unlimited** rather than a missing key, so "this plan has no
message cap" is something a reader can see rather than infer from an absence.

An unparseable entry is logged and skipped rather than raised: a malformed plan string at startup
must not take the API down. The fallback is the built-in table below, and a plan name nobody
configured falls back to the free tier — never to unlimited, since an unknown plan is a data problem
and the safe reading of a data problem is the one that does not hand out an uncapped account.

Note that the ceilings below are only *enforced* when ``BILLING_ENFORCE`` is on, which it is not by
default — see :func:`enforced`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src import configs
from src.modules.tenants.domain.models import TenantPlan

logger = logging.getLogger("api.billing.plans")

UNLIMITED = -1


@dataclass(frozen=True, slots=True)
class Plan:
    """What one plan allows. Every field is a ceiling, and -1 is no ceiling."""

    name: str
    agents: int
    messages_per_month: int
    storage_bytes: int

    def allows(self, limit: int, current: int, adding: int = 1) -> bool:
        return limit == UNLIMITED or current + adding <= limit


# Used when nothing is configured, and to fill in limits a configured entry does not name. These
# are a starting shape rather than a pricing decision — §9 leaves the pricing model open, which is
# also why enforcement is off until somebody turns it on.
DEFAULTS: dict[str, Plan] = {
    TenantPlan.FREE.value: Plan(
        name=TenantPlan.FREE.value,
        agents=1,
        messages_per_month=500,
        storage_bytes=50 * 1024 * 1024,
    ),
    TenantPlan.STARTER.value: Plan(
        name=TenantPlan.STARTER.value,
        agents=5,
        messages_per_month=10_000,
        storage_bytes=500 * 1024 * 1024,
    ),
    TenantPlan.PRO.value: Plan(
        name=TenantPlan.PRO.value,
        agents=25,
        messages_per_month=100_000,
        storage_bytes=5 * 1024 * 1024 * 1024,
    ),
}

_FIELDS = {"agents": "agents", "messages": "messages_per_month", "storage": "storage_bytes"}


def _parse_entry(entry: str) -> Plan | None:
    name, separator, limits = entry.partition("=")
    if not separator:
        logger.warning("ignoring malformed BILLING_PLANS entry %r", entry)
        return None

    values: dict[str, int] = {}
    for pair in limits.split(","):
        key, has_value, raw = pair.partition(":")
        field = _FIELDS.get(key.strip().lower())
        if not has_value or field is None:
            logger.warning("ignoring unknown limit %r in BILLING_PLANS entry %r", pair, entry)
            continue
        try:
            values[field] = int(raw)
        except ValueError:
            logger.warning("ignoring non-numeric limit %r in BILLING_PLANS entry %r", pair, entry)

    plan_name = name.strip().lower()
    base = DEFAULTS.get(plan_name)
    if base is None and len(values) < len(_FIELDS):
        logger.warning("BILLING_PLANS entry %r does not define every limit; ignoring", entry)
        return None

    # A configured entry overrides only the limits it names; the rest come from the default for
    # that plan. Raising one ceiling should not require restating the other two.
    defaults = base or Plan(plan_name, UNLIMITED, UNLIMITED, UNLIMITED)
    return Plan(
        name=plan_name,
        agents=values.get("agents", defaults.agents),
        messages_per_month=values.get("messages_per_month", defaults.messages_per_month),
        storage_bytes=values.get("storage_bytes", defaults.storage_bytes),
    )


def plans() -> dict[str, Plan]:
    """Every configured plan, falling back to the defaults for anything unnamed.

    Read afresh on each call rather than cached, for the same reason the price table is: a
    configuration reload should take effect without a restart.
    """
    configured: dict[str, Plan] = dict(DEFAULTS)
    raw: str = (configs.BILLING_PLANS or "").strip()
    for entry in raw.split(";"):
        if not entry.strip():
            continue
        plan = _parse_entry(entry)
        if plan is not None:
            configured[plan.name] = plan
    return configured


def for_tenant(plan_name: str) -> Plan:
    """The plan a tenant is on.

    A tenant carrying a plan name nobody has configured falls back to the free tier rather than to
    unlimited: an unknown plan is a data problem, and the safe reading of a data problem is the one
    that does not hand out an uncapped account.
    """
    table = plans()
    return table.get(plan_name.strip().lower()) or table[TenantPlan.FREE.value]


def enforced() -> bool:
    """Whether limits refuse work, or are merely reported.

    **Off by default.** The pricing model is §9's open question, still the maintainer's to settle,
    and a platform that enforced invented ceilings on day one would be answering it quietly. With
    enforcement off every counter still runs and ``GET /billing/plan`` still reports which ceilings
    a tenant is over — so the numbers are there the moment the plans become real, and nothing was
    refused in the meantime on the strength of a guess.
    """
    enabled: bool = configs.BILLING_ENFORCE
    return enabled
