"""Plan and usage shapes (spec §5.9).

``limit: -1`` and ``remaining: null`` both mean *unlimited*, and they appear together. The pair is
deliberate: a client that reads `remaining` to draw a progress bar needs it absent rather than
enormous, while a client that reads `limit` gets a value it can compare numerically.
"""

from __future__ import annotations

from pydantic import Field

from src.shared.responses import CamelModel


class LimitResponse(CamelModel):
    """One ceiling, what is used against it, and what is left."""

    limit: int = Field(description="What the plan allows. `-1` means unlimited.", examples=[500])
    used: int = Field(description="What is used right now, or this period.", examples=[128])
    remaining: int | None = Field(
        default=None, description="`limit - used`, floored at zero. Absent when unlimited."
    )
    exceeded: bool = Field(description="Whether the ceiling has been reached.")


class PlanResponse(CamelModel):
    """The plan, its ceilings, and this billing period's usage against them."""

    plan: str = Field(description="The plan this tenant is on.", examples=["free"])
    period: str = Field(
        description="The billing period these figures cover, as `YYYY-MM` in UTC.",
        examples=["2026-08"],
    )
    enforced: bool = Field(
        description=(
            "Whether limits refuse work. When false the figures are still counted and reported but "
            "nothing is blocked — what a deployment runs while it is settling on pricing."
        )
    )
    agents: LimitResponse = Field(description="Agents in this tenant. Deleting one frees a slot.")
    messages: LimitResponse = Field(
        description="Messages this period. Resets at the start of each month."
    )
    storage: LimitResponse = Field(
        description="Bytes of submitted knowledge stored. Deleting a source frees its bytes."
    )
    prompt_tokens: int = Field(description="Input tokens used this period.")
    completion_tokens: int = Field(description="Output tokens used this period.")
    cost_micro_usd: int = Field(
        description=(
            "Estimated provider spend this period, in millionths of a US dollar. Only messages "
            "whose model has a configured price contribute."
        ),
        examples=[1_234_500],
    )


class UsagePeriodResponse(CamelModel):
    """One closed or in-progress period's counters."""

    period: str = Field(examples=["2026-08"])
    messages: int
    prompt_tokens: int
    completion_tokens: int
    cost_micro_usd: int


class UsageHistoryResponse(CamelModel):
    periods: list[UsagePeriodResponse] = Field(
        description="Most recent period first. Periods with no usage are absent, not zero-filled."
    )
