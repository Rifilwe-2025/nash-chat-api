"""Application startup and shutdown.

Long-lived singletons (engine, provider registry, queue connections) are created here and pinned to
``app.state`` so nothing reaches for a global at request time.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src import configs

logger = logging.getLogger("api.lifespan")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "starting %s v%s (env=%s)",
        configs.APP_NAME,
        configs.APP_VERSION,
        configs.APP_ENV,
    )
    # Phase 1 pins the database engine and session factory here.
    yield
    logger.info("shutdown complete")
