"""Operational telemetry shared by the API, the worker, and the LLM layer.

Infrastructure rather than wiring, which is why it lives here and not in ``core``: ``shared/llm``
records provider latency into it and ``core/middleware`` records request latency into it, and a
dependency running from ``shared`` into ``core`` would be the wrong way round.
"""

from src.shared.observability.metrics import (
    HTTP_DURATION,
    HTTP_REQUESTS,
    PROVIDER_CALLS,
    PROVIDER_DURATION,
    PROVIDER_ERRORS,
    MetricsRegistry,
    Timing,
    metrics,
)

__all__ = [
    "HTTP_DURATION",
    "HTTP_REQUESTS",
    "PROVIDER_CALLS",
    "PROVIDER_DURATION",
    "PROVIDER_ERRORS",
    "MetricsRegistry",
    "Timing",
    "metrics",
]
