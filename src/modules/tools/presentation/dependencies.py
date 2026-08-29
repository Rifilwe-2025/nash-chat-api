"""Reaching the process-wide tool cache from a request (spec §5.2.1).

The same shape ``core/rate_limit`` uses: one instance built at startup and pinned to ``app.state``,
read here rather than reached for as a module global. A request that arrives before the lifespan has
run — which only happens in tests that build the app by hand — gets a fresh cache instead of an
error, since a cache that is empty is not a failure.

Paths with no ``app`` at all (the Celery worker) simply do not pass one, and the tool service then
does not cache. That is the right trade rather than a gap: a worker handling one WhatsApp message
per task has nothing to reuse a cached response with.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from src.modules.tools.internal.cache import ResponseCache


def get_tool_cache(request: Request) -> ResponseCache:
    cache: ResponseCache | None = getattr(request.app.state, "tool_cache", None)
    if cache is None:  # pragma: no cover - lifespan always sets this
        cache = ResponseCache()
        request.app.state.tool_cache = cache
    return cache


ToolCacheDep = Annotated[ResponseCache, Depends(get_tool_cache)]
