from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, Request, Response

from src.modules.system.domain.services import SystemService
from src.modules.system.presentation.dtos.health import HealthResponse, ReadinessResponse
from src.shared.responses import ApiResponse, create_router

router = create_router(tags=["system"])


def get_system_service(request: Request) -> SystemService:
    return SystemService(
        engine=getattr(request.app.state, "engine", None),
        redis=getattr(request.app.state, "redis", None),
    )


SystemServiceDep = Annotated[SystemService, Depends(get_system_service)]


@router.get(
    "/health",
    response_model=ApiResponse[HealthResponse],
    summary="Liveness probe",
    description=(
        "Reports that the process is running and serving, along with its name, version and "
        "environment. Touches no dependencies, so it stays green while Postgres or Redis are "
        "down — use `/health/ready` to gate traffic."
    ),
    responses={200: {"description": "The process is serving."}},
)
def health(service: SystemServiceDep) -> ApiResponse[HealthResponse]:
    return ApiResponse.ok(service.health())


@router.get(
    "/health/ready",
    response_model=ApiResponse[ReadinessResponse],
    summary="Readiness probe",
    description=(
        "Checks every dependency the API needs to do work — Postgres with `SELECT 1` and Redis "
        "with `PING` — and reports each outcome individually. Returns **503** when any dependency "
        "is unhealthy, with the failing dependency named in the payload."
    ),
    responses={
        200: {"description": "Every dependency answered."},
        503: {"description": "At least one dependency is unavailable."},
    },
)
async def readiness(
    service: SystemServiceDep, response: Response
) -> ApiResponse[ReadinessResponse]:
    result = await service.readiness()
    if not result.ready:
        response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
    return ApiResponse.ok(result)
