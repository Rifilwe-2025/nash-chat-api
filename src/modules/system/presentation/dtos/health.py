from __future__ import annotations

from src.shared.responses import CamelModel


class HealthResponse(CamelModel):
    status: str
    name: str
    version: str
    environment: str
