from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from src.modules.system.domain.services import SystemService
from src.modules.system.presentation.dtos.health import HealthResponse
from src.shared.responses import ApiResponse, create_router

router = create_router(tags=["system"])


def get_system_service() -> SystemService:
    return SystemService()


SystemServiceDep = Annotated[SystemService, Depends(get_system_service)]


@router.get(
    "/health",
    response_model=ApiResponse[HealthResponse],
    summary="Service health",
    description=(
        "Reports that the service is running, along with its name, version and environment. "
        "Use this for liveness checks; readiness probes arrive with the database layer."
    ),
    responses={200: {"description": "The service is up."}},
)
def health(service: SystemServiceDep) -> ApiResponse[HealthResponse]:
    return ApiResponse.ok(service.health())
