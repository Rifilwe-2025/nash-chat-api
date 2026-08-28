"""Health and readiness reporting.

Liveness answers "is the process serving"; readiness answers "can it actually do work", which means
touching Postgres and Redis. A failing dependency is reported, never raised — the caller needs the
detail to know *what* is down.
"""

from __future__ import annotations

import logging

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from src import configs
from src.modules.system.presentation.dtos.health import (
    DependencyStatus,
    HealthResponse,
    ReadinessResponse,
)

logger = logging.getLogger("api.system")


class SystemService:
    def __init__(self, engine: AsyncEngine | None = None, redis: Redis | None = None) -> None:
        self._engine = engine
        self._redis = redis

    def health(self) -> HealthResponse:
        return HealthResponse(
            status="ok",
            name=configs.APP_NAME,
            version=configs.APP_VERSION,
            environment=configs.APP_ENV,
        )

    async def readiness(self) -> ReadinessResponse:
        dependencies = [await self._check_postgres(), await self._check_redis()]
        return ReadinessResponse(
            ready=all(dependency.healthy for dependency in dependencies),
            dependencies=dependencies,
        )

    async def _check_postgres(self) -> DependencyStatus:
        if self._engine is None:
            return DependencyStatus(name="postgres", healthy=False, detail="engine not configured")
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as exc:
            logger.warning("postgres readiness check failed: %s", exc)
            return DependencyStatus(name="postgres", healthy=False, detail=_reason(exc))
        return DependencyStatus(name="postgres", healthy=True)

    async def _check_redis(self) -> DependencyStatus:
        if self._redis is None:
            return DependencyStatus(name="redis", healthy=False, detail="client not configured")
        try:
            await self._redis.ping()
        except Exception as exc:
            logger.warning("redis readiness check failed: %s", exc)
            return DependencyStatus(name="redis", healthy=False, detail=_reason(exc))
        return DependencyStatus(name="redis", healthy=True)


def _reason(exc: Exception) -> str:
    """First line only — readiness detail must never carry a driver stack trace."""
    return str(exc).strip().splitlines()[0][:200] or type(exc).__name__
