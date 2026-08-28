"""Retry and fallback policy, written once for every provider (spec §5.3).

Two separate mechanisms, deliberately not conflated:

* **Retry** — the same provider, again, after a backoff. For transient failures (timeouts, 5xx,
  rate limits) where the request itself is fine.
* **Fallback** — a *different* provider, once retries are exhausted on a rate limit or outage. A
  tenant losing an answer because one vendor is saturated is a worse outcome than answering on the
  configured second choice.

Provider SDK-level retries are disabled so that backoff, jitter, and the fallback decision all
happen in one place rather than being split between the SDK and this module.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from src.shared.llm.errors import LLMError, LLMRateLimitError

logger = logging.getLogger("api.llm")

T = TypeVar("T")


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full jitter — spreads retries instead of synchronising them."""
    ceiling = min(cap, base * (2**attempt))
    return random.uniform(0, ceiling)


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay: float,
    max_delay: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run ``operation``, retrying only failures marked retryable.

    A rate limit's ``retry-after`` is honoured when the provider sends one, since guessing a
    shorter delay just earns another rejection.
    """
    last_error: LLMError | None = None

    for attempt in range(attempts):
        try:
            return await operation()
        except LLMError as exc:
            if not exc.retryable or attempt == attempts - 1:
                raise
            last_error = exc
            delay = backoff_delay(attempt, base_delay, max_delay)
            if isinstance(exc, LLMRateLimitError) and exc.retry_after:
                delay = max(delay, exc.retry_after)
            logger.warning(
                "llm call failed (%s), retry %d/%d in %.2fs",
                type(exc).__name__,
                attempt + 1,
                attempts - 1,
                delay,
            )
            await sleep(delay)

    raise last_error if last_error else RuntimeError("retry loop exited without result")
