"""Process-wide operational counters (spec §5.8, monitoring and failure tracking).

**This is not the analytics module.** The two answer different questions for different people, and
keeping them apart is what stops either from being wrong:

* ``modules/analytics`` answers *a tenant's* questions — how many messages my agent handled, what it
  cost me, which of my sources failed — from rows in the database, scoped to that tenant, durable
  across restarts and reconcilable against the transcript.
* This registry answers *the operator's* questions — is the API slow, is a provider erroring, how
  many requests is this process serving — across every tenant, in memory, for this process only.

In-process on purpose. A metric written to Postgres would put a write on every request path to
record that the request happened: a cost paid on the hot path for a number nobody reads between
deploys. Counters are cheap and lossy on restart, which is the right trade for telemetry — anything
a tenant may need later lives in a table instead.

**Cardinality is capped.** A series is a ``(name, labels)`` pair, and labels come from request
paths, provider names and status codes. An unbounded path (``/agents/<uuid>``) would grow one series
per agent until the process ran out of memory, so routes are recorded by their *template* and
:data:`MAX_SERIES` is the backstop: past it new series are dropped and counted rather than admitted.
A metric that quietly stops recording is better than one that takes the API down with it.

Not Prometheus, and not pretending to be. If a deployment wants scraping, :meth:`MetricsRegistry.
snapshot` is the shape an exporter renders — a deployment concern, not a reason to take on a
dependency here.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

# Past this many distinct (name, labels) series the registry stops admitting new ones. Ten thousand
# small records is a few megabytes; an unbounded label is unbounded memory.
MAX_SERIES = 10_000

# Labels are normalised to strings, so 200 and "200" cannot become two series for one status.
Labels = tuple[tuple[str, str], ...]


@dataclass(slots=True)
class Timing:
    """Count, total and worst case for one timed series.

    Deliberately not a histogram. Buckets have to be chosen in advance, and a wrong choice reads as
    confidently precise while saying nothing useful; count, mean and max answer "is it slow, and how
    bad does it get" without inviting anyone to trust a percentile that was never measured.
    """

    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0

    def observe(self, duration_ms: float) -> None:
        self.count += 1
        self.total_ms += duration_ms
        self.max_ms = max(self.max_ms, duration_ms)

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0


def _labels(values: dict[str, Any]) -> Labels:
    """Sorted string pairs, so label order cannot split one series into two."""
    return tuple(
        sorted((str(key), str(value)) for key, value in values.items() if value is not None)
    )


@dataclass(slots=True)
class MetricsRegistry:
    """Counters and timings for one process.

    Guarded by a lock rather than trusting the GIL: ``+= 1`` on a dictionary value is not atomic,
    and the API serves requests from a thread pool as well as from the event loop. The lock covers a
    dictionary lookup and an addition, which is not a contention point at any traffic this platform
    will see.
    """

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _counters: dict[tuple[str, Labels], int] = field(default_factory=dict)
    _timings: dict[tuple[str, Labels], Timing] = field(default_factory=dict)
    _dropped: int = 0
    started_at: float = field(default_factory=time.time)

    # -- recording -----------------------------------------------------------

    def increment(self, name: str, amount: int = 1, **labels: Any) -> None:
        key = (name, _labels(labels))
        with self._lock:
            if key not in self._counters and self._full():
                self._dropped += 1
                return
            self._counters[key] = self._counters.get(key, 0) + amount

    def observe(self, name: str, duration_ms: float, **labels: Any) -> None:
        key = (name, _labels(labels))
        with self._lock:
            timing = self._timings.get(key)
            if timing is None:
                if self._full():
                    self._dropped += 1
                    return
                timing = self._timings[key] = Timing()
            timing.observe(duration_ms)

    @contextmanager
    def timed(self, name: str, **labels: Any) -> Iterator[None]:
        """Time a block, recording it even when the block raises.

        A provider call that fails slowly is exactly the one worth timing — dropping the measurement
        on the exception path would hide every timeout in the system.
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, (time.perf_counter() - started) * 1000, **labels)

    # -- reading -------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """A serialisable view of everything recorded since this process started."""
        with self._lock:
            counters = [
                {"name": name, "labels": dict(labels), "value": value}
                for (name, labels), value in sorted(self._counters.items())
            ]
            timings = [
                {
                    "name": name,
                    "labels": dict(labels),
                    "count": timing.count,
                    "meanMs": round(timing.mean_ms, 2),
                    "maxMs": round(timing.max_ms, 2),
                }
                for (name, labels), timing in sorted(self._timings.items())
            ]
            dropped = self._dropped
            started = self.started_at

        return {
            "uptimeSeconds": round(time.time() - started, 1),
            "counters": counters,
            "timings": timings,
            "seriesDropped": dropped,
        }

    def counter(self, name: str, **labels: Any) -> int:
        """One counter's current value. For tests and for readers that want a single number."""
        with self._lock:
            return self._counters.get((name, _labels(labels)), 0)

    def timing(self, name: str, **labels: Any) -> Timing | None:
        with self._lock:
            return self._timings.get((name, _labels(labels)))

    def reset(self) -> None:
        """Clear everything. For tests — a process never resets its own telemetry."""
        with self._lock:
            self._counters.clear()
            self._timings.clear()
            self._dropped = 0
            self.started_at = time.time()

    def _full(self) -> bool:
        return len(self._counters) + len(self._timings) >= MAX_SERIES


# The process registry. A module-level singleton rather than something pinned to ``app.state``,
# because the Celery worker records into it too and has no application object — and unlike a
# database session, a counter has no lifecycle to manage.
metrics = MetricsRegistry()

# Metric names live here so a rename cannot desynchronise the producer from the reader.
HTTP_REQUESTS = "http_requests_total"
HTTP_DURATION = "http_request_duration_ms"
PROVIDER_CALLS = "llm_provider_calls_total"
PROVIDER_DURATION = "llm_provider_duration_ms"
PROVIDER_ERRORS = "llm_provider_errors_total"

__all__ = [
    "HTTP_DURATION",
    "HTTP_REQUESTS",
    "MAX_SERIES",
    "PROVIDER_CALLS",
    "PROVIDER_DURATION",
    "PROVIDER_ERRORS",
    "MetricsRegistry",
    "Timing",
    "metrics",
]
