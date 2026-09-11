"""Analytics: usage, cost, quality, and what failed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from src.modules.analytics.domain.services import reporting_window
from src.modules.mcp.domain.models import McpScope
from src.modules.mcp.presentation.mcp import serializers as render
from src.modules.mcp.presentation.mcp.annotations import READ_ONLY
from src.modules.mcp.presentation.mcp.context import workspace

Days = Annotated[int, Field(ge=1, le=366, description="How many days back from now to report on.")]


def register(server: MCPServer[Any]) -> None:
    @server.tool(title="Get usage report", annotations=READ_ONLY)
    async def get_usage_report(
        agent_id: Annotated[
            uuid.UUID | None, Field(description="Report on this agent only. Omit for everything.")
        ] = None,
        days: Days = 30,
        include_preview: Annotated[
            bool, Field(description="Count preview (test) traffic as well as real customers.")
        ] = False,
    ) -> dict[str, Any]:
        """Report conversations, messages, tokens, cost and quality over a window.

        Quality signals: `fallbackRate` is the share of answers given without relevant knowledge,
        `escalationRate` the share of conversations handed to a human. Cost is recorded only for
        models the deployment has prices for.
        """
        async with workspace(McpScope.READ) as ws:
            window = reporting_window(start=datetime.now(UTC) - timedelta(days=days))
            report = await ws.analytics.report(
                window, agent_id=agent_id, include_preview=include_preview
            )
            result: dict[str, Any] = render.camelised(render.jsonable(report))
            return result

    @server.tool(title="Get failure report", annotations=READ_ONLY)
    async def get_failure_report(
        days: Days = 7,
        recent_limit: Annotated[
            int, Field(ge=1, le=50, description="How many recent examples to return per kind.")
        ] = 10,
    ) -> dict[str, Any]:
        """Report everything that failed: ingestion, provider errors, webhooks, channels and tools.

        Each kind has a count and its most recent examples — a good first stop when an agent is
        misbehaving.
        """
        async with workspace(McpScope.READ) as ws:
            window = reporting_window(start=datetime.now(UTC) - timedelta(days=days))
            report = await ws.analytics.failures(window, recent_limit)
            result: dict[str, Any] = render.camelised(render.jsonable(report))
            result["total"] = report.total
            return result
