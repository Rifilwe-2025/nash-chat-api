"""Who this connection is."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from src.modules.mcp.domain.models import McpScope
from src.modules.mcp.presentation.mcp import serializers as render
from src.modules.mcp.presentation.mcp.annotations import READ_ONLY
from src.modules.mcp.presentation.mcp.context import workspace


def register(server: MCPServer[Any]) -> None:
    @server.tool(title="Who am I", annotations=READ_ONLY)
    async def whoami() -> dict[str, Any]:
        """Show the account, organisation and scopes this connection acts with.

        Call this first to confirm the connection works. `canWrite` says whether this token may
        create and change things, or only read them.
        """
        async with workspace(McpScope.READ) as ws:
            tenant = await ws.accounts.get_tenant(ws.tenant_id)
            principal = ws.principal
            return render.json_object(
                {
                    "user": {"id": principal.user_id, "email": principal.email},
                    "organisation": {
                        "id": tenant.id,
                        "name": tenant.name,
                        "status": tenant.status,
                    },
                    "scopes": sorted(principal.scopes),
                    "canWrite": principal.allows(McpScope.WRITE),
                }
            )
