"""Channels and integration: where an agent is reachable, and how to wire it into a product."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from src.modules.channels.domain.models import ChannelType
from src.modules.mcp.domain.models import McpScope
from src.modules.mcp.presentation.mcp import serializers as render
from src.modules.mcp.presentation.mcp.annotations import READ_ONLY, UPDATES
from src.modules.mcp.presentation.mcp.context import current_request, workspace
from src.modules.mcp.presentation.mcp.params import AgentId, PageNumber, PageSize
from src.shared.database.pagination import PageRequest

#: Where the web channel keeps its browser allowlist. Named here rather than imported from the
#: channels module's ``internal/``, which is private; the channel service validates the value.
ALLOWED_ORIGINS = "allowedOrigins"


def register(server: MCPServer[Any]) -> None:
    @server.tool(title="Get integration guide", annotations=READ_ONLY)
    async def get_integration_guide(agent_id: AgentId) -> dict[str, Any]:
        """Get the complete guide for integrating an agent into a website or app, in Markdown.

        Generated from the live API schema: quickstart, authentication with an agent API key, the
        session model, streaming frames, escalation, allowed origins, webhooks, rate limits, error
        codes and every public endpoint. Read this before writing integration code. The API key it
        refers to is issued by the user in the web console.
        """
        request = current_request()
        async with workspace(McpScope.READ) as ws:
            agent, markdown = await ws.channels.integration_guide(
                agent_id, base_url=request.base_url, schema=request.openapi()
            )
            return render.json_object(
                {
                    "agentId": agent.id,
                    "agentName": agent.name,
                    "baseUrl": request.base_url,
                    "markdown": markdown,
                }
            )

    @server.tool(title="List agent channels", annotations=READ_ONLY)
    async def list_channels(agent_id: AgentId) -> dict[str, Any]:
        """List an agent's channel configurations (web, WhatsApp) and their settings.

        Channel credentials, such as a WhatsApp access token, are never returned.
        """
        async with workspace(McpScope.READ) as ws:
            configs = await ws.channels.list_configs(agent_id)
            return {"items": [render.channel_config(config) for config in configs]}

    @server.tool(title="Set web allowed origins", annotations=UPDATES)
    async def set_web_allowed_origins(
        agent_id: AgentId,
        allowed_origins: Annotated[
            list[str],
            Field(
                description=(
                    "The complete list of browser origins allowed to call this agent, each a "
                    "scheme and host only, such as `https://shop.example.com`. An empty list "
                    "allows any origin. Replaces the current list."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Set which websites may embed an agent's chat from the browser.

        Requests from a server carry no origin and are unaffected — the API key is still the
        credential. Other web channel settings are kept.
        """
        async with workspace(McpScope.WRITE) as ws:
            existing = next(
                (
                    config
                    for config in await ws.channels.list_configs(agent_id)
                    if config.channel_type is ChannelType.WEB
                ),
                None,
            )
            settings: dict[str, object] = dict(existing.settings_json) if existing else {}
            settings[ALLOWED_ORIGINS] = allowed_origins
            config = await ws.channels.configure(agent_id, ChannelType.WEB, settings=settings)
            return render.channel_config(config)

    @server.tool(title="List API keys", annotations=READ_ONLY)
    async def list_api_keys(
        agent_id: Annotated[uuid.UUID | None, Field(description="Only this agent's keys.")] = None,
        page: PageNumber = 1,
        page_size: PageSize = 20,
    ) -> dict[str, Any]:
        """List agent API keys — the credentials a website or app uses to call the chat API.

        Metadata only: name, prefix, scopes, rate limit and last use. Secrets are never returned,
        and keys are issued and revoked by the user in the web console.
        """
        async with workspace(McpScope.READ) as ws:
            result = await ws.api_keys.list_keys(PageRequest(page, page_size), agent_id=agent_id)
            return render.page(result, render.api_key)
