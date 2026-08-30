"""Usage metering (spec §5.9, "usage-based billing hooks (token/message metering)").

One table, and its shape is the whole design: **a counter per tenant, per billing period, per
metric**, incremented as work happens.

**Why a counter and not a sum over `message`.** The obvious alternative is to bill from the rows
that already exist — count the messages in a month, add up their tokens. That works until the first
time it matters. A billing figure must not change after the fact, and message rows do: a
conversation deleted by a tenant, a cascade from a removed agent, a retention policy. Reading a
bill from mutable rows produces an invoice that disagrees with the one sent last month, and nobody
can say which was right. A counter only ever goes up, and once a period closes its row is a fact.

It is also the difference between a quota check being one indexed read and being an aggregate over
the largest table in the schema — on the request path, on every message.

**Period is a `YYYY-MM` string, not a date range.** Billing periods are calendar months here (§5.9
puts billing after the core product, so there is no proration, no anniversary dates, no trial
windows). A string that reads as a month is what a person checking an invoice actually wants, and
the ordering is the same as the chronological one.

**Metering is not billing.** Nothing in this module charges anybody. It counts what was used and
enforces the plan's ceilings; the invoice, the payment provider, and the pricing model itself are
the open question §9 leaves for the maintainer.
"""

from __future__ import annotations

import enum

from sqlalchemy import BigInteger, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base_model import TenantScopedModel, enum_column


class UsageMetric(str, enum.Enum):
    """What is counted.

    ``MESSAGES`` is the billable unit a plan is sold in. ``PROMPT_TOKENS``, ``COMPLETION_TOKENS``
    and ``COST_MICRO_USD`` are what the messages *cost us*, kept apart because they answer a
    different question: whether a plan's price covers the traffic it allows.

    Storage and agent count are deliberately absent. Both are *states* rather than accumulations —
    a deleted source frees its bytes, a deleted agent frees its slot — and a counter that only goes
    up is the wrong instrument for either. They are measured where they live, by the modules that
    own them.
    """

    MESSAGES = "messages"
    PROMPT_TOKENS = "prompt_tokens"
    COMPLETION_TOKENS = "completion_tokens"
    COST_MICRO_USD = "cost_micro_usd"


class UsageCounter(TenantScopedModel):
    """One tenant's running total of one metric in one billing period."""

    __tablename__ = "usage_counter"
    __table_args__ = (
        # The uniqueness that makes the increment an upsert: one row per (tenant, period, metric),
        # so concurrent turns add to the same row instead of racing to create two.
        UniqueConstraint(
            "tenant_id", "period", "metric", name="uq_usage_counter_tenant_id_period_metric"
        ),
        # Reading a tenant's usage history is "their rows, newest period first".
        Index("ix_usage_counter_tenant_period", "tenant_id", "period"),
    )

    # `YYYY-MM`. Fixed width, sorts chronologically as a string, and reads as a month to a person
    # looking at an invoice.
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    metric: Mapped[UsageMetric] = mapped_column(enum_column(UsageMetric, "usage_metric"))
    # BigInteger because token counts across a busy month overflow a 32-bit integer sooner than
    # anyone expects — a hundred thousand messages at 2,000 prompt tokens is already 200 million.
    value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
