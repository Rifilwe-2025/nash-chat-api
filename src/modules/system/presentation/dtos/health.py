from __future__ import annotations

from pydantic import Field

from src.shared.responses import CamelModel


class HealthResponse(CamelModel):
    status: str = Field(description="Always 'ok' when the process is serving.", examples=["ok"])
    name: str = Field(description="Configured application name.", examples=["Nash Chat API"])
    version: str = Field(description="Running application version.", examples=["0.1.0"])
    environment: str = Field(
        description="Deployment environment.", examples=["local", "production"]
    )


class DependencyStatus(CamelModel):
    name: str = Field(description="Dependency identifier.", examples=["postgres", "redis"])
    healthy: bool = Field(description="Whether the dependency answered successfully.")
    detail: str | None = Field(
        default=None,
        description="Failure reason when the check did not succeed.",
        examples=["connection refused"],
    )


class ReadinessResponse(CamelModel):
    ready: bool = Field(description="True only when every dependency is healthy.")
    dependencies: list[DependencyStatus] = Field(
        description="Per-dependency outcome, in the order they were checked."
    )
