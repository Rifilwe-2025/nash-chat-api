"""Health reporting.

Readiness gains real dependency checks (Postgres, Redis) in Phase 1; for now the service reports
the running application's identity so the envelope and wiring are verifiable end to end.
"""

from __future__ import annotations

from src import configs
from src.modules.system.presentation.dtos.health import HealthResponse


class SystemService:
    def health(self) -> HealthResponse:
        return HealthResponse(
            status="ok",
            name=configs.APP_NAME,
            version=configs.APP_VERSION,
            environment=configs.APP_ENV,
        )
