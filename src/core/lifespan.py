"""Application startup and shutdown.

Long-lived singletons (the database engine, its session factory, the Redis client) are created here
and pinned to ``app.state`` so nothing reaches for a module-level global at request time.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI

from src import configs
from src.core.rate_limit import build_limiter

# Reaching into a module's `internal/` from a composition root, as `worker.py` does for its tasks:
# startup wiring is where a module's machinery is hooked up, and the alternative — a public service
# method that only the application factory may call — would be a wider surface, not a smaller one.
from src.modules.admin.internal.bootstrap import (
    ensure_bootstrap_admin,
    warn_if_handover_password_unchanged,
)
from src.modules.tools.internal.cache import ResponseCache
from src.shared.crypto import warn_if_unprotected
from src.shared.database.engine import create_engine, create_session_factory

logger = logging.getLogger("api.lifespan")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "starting %s v%s (env=%s)",
        configs.APP_NAME,
        configs.APP_VERSION,
        configs.APP_ENV,
    )

    # Says so loudly when tenant credentials are being written in clear (spec §5.7). At startup
    # rather than on first use: an operator should learn this from the boot log, not from a
    # database dump.
    warn_if_unprotected()

    engine = create_engine()
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.redis = aioredis.from_url(  # type: ignore[no-untyped-call]
        configs.REDIS_URL, decode_responses=True
    )
    app.state.rate_limiter = build_limiter()
    # One tool-response cache per process, so a repeated identical lookup within a few seconds
    # is not paid for twice. In-process on purpose — see tools/internal/cache.py.
    app.state.tool_cache = ResponseCache()

    # The first platform administrator, from the environment. Idempotent — it only ever creates —
    # and it never stops the API from starting, because a deployment with no administrator is a
    # thing to fix while one that will not boot is an outage.
    async with app.state.session_factory() as session:
        await ensure_bootstrap_admin(session)
        await warn_if_handover_password_unchanged(session)

    try:
        yield
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        logger.info("shutdown complete")
