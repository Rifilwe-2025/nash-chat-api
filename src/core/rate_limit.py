"""Per-key rate limiting (spec §5.6).

A **fixed window** counter, not a token bucket: the limit a tenant is sold is "N requests per
minute", and a fixed window is the algorithm that means exactly that. Its known weakness is a burst
straddling a boundary, which can pass up to 2N in a sliding minute — acceptable here, where the
limit exists to stop runaway integrations and unbounded provider spend, not to shape traffic to the
millisecond.

Two backends. **Redis** is the correct one in any real deployment: the API runs as more than one
worker and a limit counted per process is not the limit that was sold. The **in-memory** backend is
the default for local development and tests, and says so loudly rather than pretending to be
distributed.

The response headers follow the `X-RateLimit-*` convention every HTTP client already understands,
plus `Retry-After` on a 429, because §10 requires a developer to integrate against these docs
without support — and "why am I getting 429" must be answerable from the response alone.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol

import redis.asyncio as redis

from src import configs

logger = logging.getLogger("api.rate_limit")

WINDOW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class RateLimitVerdict:
    allowed: bool
    limit: int
    remaining: int
    reset_at: int

    @property
    def retry_after(self) -> int:
        return max(self.reset_at - int(time.time()), 1)

    def headers(self) -> dict[str, str]:
        """What every response carries, allowed or not — a client should not have to guess."""
        values = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_at),
        }
        if not self.allowed:
            values["Retry-After"] = str(self.retry_after)
        return values


class RateLimitBackend(Protocol):
    async def increment(self, key: str, window: int) -> int:
        """Count one hit and return the running total for the current window."""
        ...


class InMemoryBackend:
    """Per-process counters. Correct only when there is exactly one process.

    Deliberately not silent about that: a deployment that leaves this on is not enforcing the limit
    it thinks it is, so selecting it outside local use logs a warning at startup.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[str, int], int] = {}

    async def increment(self, key: str, window: int) -> int:
        bucket = int(time.time()) // window
        # Old buckets are dropped rather than swept on a timer: the dictionary only ever holds the
        # keys seen in the current and previous window.
        self._counts = {
            (name, slot): count
            for (name, slot), count in self._counts.items()
            if slot >= bucket - 1
        }
        current = self._counts.get((key, bucket), 0) + 1
        self._counts[(key, bucket)] = current
        return current


class RedisBackend:
    """Shared counters, so the limit holds across every worker."""

    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    async def increment(self, key: str, window: int) -> int:
        bucket = int(time.time()) // window
        redis_key = f"ratelimit:{key}:{bucket}"
        pipeline = self._client.pipeline()
        pipeline.incr(redis_key)
        # Expiry is set every time rather than only on creation: one command instead of two, and a
        # key can never outlive its window even if a previous EXPIRE was lost.
        pipeline.expire(redis_key, window * 2)
        result = await pipeline.execute()
        return int(result[0])


class RateLimiter:
    """Applies a per-key limit through whichever backend is configured."""

    def __init__(self, backend: RateLimitBackend, window: int = WINDOW_SECONDS) -> None:
        self._backend = backend
        self._window = window

    async def check(self, key: str, limit: int) -> RateLimitVerdict:
        """Count this request against ``key`` and say whether it may proceed.

        A limit of zero or less means unlimited — the shape a plan without a cap takes.
        """
        now = int(time.time())
        reset_at = ((now // self._window) + 1) * self._window

        if limit <= 0:
            return RateLimitVerdict(True, limit=0, remaining=0, reset_at=reset_at)

        try:
            used = await self._backend.increment(key, self._window)
        except Exception:
            # A limiter outage must not take the API down with it. Failing open is the right
            # direction: the alternative is that a Redis blip rejects every customer's traffic.
            logger.warning("rate limit backend unavailable; allowing the request", exc_info=True)
            return RateLimitVerdict(True, limit=limit, remaining=limit, reset_at=reset_at)

        return RateLimitVerdict(
            allowed=used <= limit,
            limit=limit,
            remaining=max(limit - used, 0),
            reset_at=reset_at,
        )


def build_limiter() -> RateLimiter:
    """Construct the limiter named by configuration."""
    backend_name = (configs.RATE_LIMIT_BACKEND or "memory").strip().lower()

    if backend_name == "redis":
        client = redis.from_url(configs.REDIS_URL)  # type: ignore[no-untyped-call]
        return RateLimiter(RedisBackend(client))

    if configs.APP_ENV not in {"local", "test"}:
        logger.warning(
            "rate limiting is using the in-memory backend in the %r environment — limits are "
            "counted per worker and will not hold across processes. Set RATE_LIMIT_BACKEND=redis.",
            configs.APP_ENV,
        )
    return RateLimiter(InMemoryBackend())
