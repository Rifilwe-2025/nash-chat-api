"""Readiness reporting, including how it degrades when a dependency is down."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from src.modules.system.domain.services import SystemService


class _FailingRedis:
    async def ping(self) -> bool:
        raise ConnectionError("Error 111 connecting to localhost:6379. Connection refused.")


class _WorkingRedis:
    async def ping(self) -> bool:
        return True


async def test_readiness_reports_each_dependency(engine: AsyncEngine) -> None:
    service = SystemService(engine=engine, redis=_WorkingRedis())  # type: ignore[arg-type]

    result = await service.readiness()

    assert result.ready is True
    assert {d.name for d in result.dependencies} == {"postgres", "redis"}
    assert all(d.healthy for d in result.dependencies)


async def test_readiness_marks_the_failing_dependency(engine: AsyncEngine) -> None:
    service = SystemService(engine=engine, redis=_FailingRedis())  # type: ignore[arg-type]

    result = await service.readiness()

    assert result.ready is False
    redis_status = next(d for d in result.dependencies if d.name == "redis")
    postgres_status = next(d for d in result.dependencies if d.name == "postgres")
    assert postgres_status.healthy is True
    assert redis_status.healthy is False
    assert redis_status.detail is not None


async def test_readiness_reports_unconfigured_dependencies() -> None:
    service = SystemService(engine=None, redis=None)

    result = await service.readiness()

    assert result.ready is False
    assert all(not d.healthy for d in result.dependencies)


async def test_readiness_endpoint_returns_503_when_degraded(client: AsyncClient) -> None:
    """The test app has no Redis client configured, so readiness must fail closed."""
    response = await client.get("/health/ready")
    body: dict[str, Any] = response.json()

    assert response.status_code == 503
    assert body["success"] is True
    assert body["value"]["ready"] is False


async def test_liveness_stays_green_while_dependencies_are_down(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["value"]["status"] == "ok"
