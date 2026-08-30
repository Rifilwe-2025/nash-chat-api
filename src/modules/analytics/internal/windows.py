"""The reporting window every analytics query is bounded by.

Every number this module returns is "over some period", and leaving that period implicit is how a
dashboard ends up disagreeing with itself: one figure counted since the beginning of time, another
since Monday. One window object is built at the edge, passed to every query in the request, and
echoed back in the response so a reader can see exactly what was counted.

**Bounded on purpose.** A tenant asking for five years of messages is asking for a sequential scan
over the largest table in the schema, on a request path shared with everyone else. The span is
capped, and the cap is a refusal rather than a silent truncation — a chart labelled "last 5 years"
that quietly shows one is worse than an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src import configs
from src.shared.exceptions import ValidationException


@dataclass(frozen=True, slots=True)
class Window:
    """A half-open ``[start, end)`` interval in UTC.

    Half-open because a day boundary belongs to exactly one bucket: with a closed interval a message
    written at midnight is counted in both the day that ended and the day that began, and two
    adjacent windows no longer sum to the whole.
    """

    start: datetime
    end: datetime

    @property
    def days(self) -> int:
        return max((self.end - self.start).days, 1)


def _as_utc(value: datetime) -> datetime:
    """Naive input is read as UTC rather than as the server's local time.

    A datetime with no offset is ambiguous, and resolving it against whatever timezone the container
    happens to be in would make the same request return different numbers in two deployments.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def resolve(start: datetime | None, end: datetime | None) -> Window:
    """Build the window from what the caller supplied, filling in what they did not."""
    resolved_end = _as_utc(end) if end is not None else datetime.now(UTC)
    default_days: int = configs.ANALYTICS_DEFAULT_WINDOW_DAYS
    resolved_start = (
        _as_utc(start) if start is not None else resolved_end - timedelta(days=default_days)
    )

    if resolved_start >= resolved_end:
        raise ValidationException(
            "The start of the window must be before its end.",
            code="ANALYTICS_WINDOW_INVALID",
        )

    max_days: int = configs.ANALYTICS_MAX_WINDOW_DAYS
    if (resolved_end - resolved_start) > timedelta(days=max_days):
        raise ValidationException(
            f"A reporting window may span at most {max_days} days.",
            code="ANALYTICS_WINDOW_TOO_LONG",
        )

    return Window(start=resolved_start, end=resolved_end)


def rate(numerator: int, denominator: int) -> float:
    """A ratio in ``[0, 1]``, or zero when there is nothing to divide by.

    Zero rather than ``None``: "no messages, so no fallbacks" is honestly a fallback rate of zero,
    and a null here would force every consumer to special-case an empty day.
    """
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)
