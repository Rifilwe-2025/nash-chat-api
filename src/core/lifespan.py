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

    engine = create_engine()
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.redis = aioredis.from_url(  # type: ignore[no-untyped-call]
        configs.REDIS_URL, decode_responses=True
    )

    try:
        yield
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        logger.info("shutdown complete")
