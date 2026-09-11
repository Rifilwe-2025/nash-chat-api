"""Agents: configuration, lifecycle, and version history."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from src.modules.agents.domain.models import ModelProvider
from src.modules.agents.presentation.dtos.agent import EngagementRules, Guardrails, ModelSettings
from src.modules.mcp.domain.models import McpScope
from src.modules.mcp.presentation.mcp import serializers as render
from src.modules.mcp.presentation.mcp.annotations import CREATES, READ_ONLY, UPDATES
from src.modules.mcp.presentation.mcp.context import workspace
from src.modules.mcp.presentation.mcp.params import AgentId, PageNumber, PageSize
from src.shared.database.pagination import PageRequest

Name = Annotated[
    str,
    Field(
        min_length=1, max_length=255, description="Display name, unique within the organisation."
    ),
]
Persona = Annotated[
    str,
    Field(
        max_length=8000,
        description=(
            "Who the agent is and what it is there to do. This is the core of its system prompt."
        ),
    ),
]
Rules = Annotated[
    EngagementRules | None,
    Field(description="Tone, style, do's and don'ts, and the situations that escalate to a human."),
]
Rails = Annotated[
    Guardrails | None,
    Field(
        description=(
            "Restricted topics, the fallback reply, and whether answers must come only from the "
            "agent's knowledge."
        )
    ),
]
Provider = Annotated[
    ModelProvider | None, Field(description="The LLM provider. Required before publishing.")
]
Settings = Annotated[
    ModelSettings | None,
    Field(description="Model identifier, temperature and max tokens. Required before publishing."),
]


def register(server: MCPServer[Any]) -> None:
    @server.tool(title="List agents", annotations=READ_ONLY)
    async def list_agents(page: PageNumber = 1, page_size: PageSize = 20) -> dict[str, Any]:
        """List the agents in the organisation, newest first.

        Returns summaries: status (draft, published or paused), version, provider and model. Use
        get_agent for one agent's full configuration.
        """
        async with workspace(McpScope.READ) as ws:
            result = await ws.agents.list_agents(PageRequest(page, page_size))
            return render.page(result, render.agent_summary)

    @server.tool(title="Get agent", annotations=READ_ONLY)
    async def get_agent(agent_id: AgentId) -> dict[str, Any]:
        """Get one agent's full configuration: persona, engagement rules, guardrails and model.

        `hasModelApiKey` says whether the agent carries its own provider key. The key itself is
        never returned, and is set only in the web console.
        """
        async with workspace(McpScope.READ) as ws:
            return render.agent(await ws.agents.get(agent_id))

    @server.tool(title="List agent versions", annotations=READ_ONLY)
    async def list_agent_versions(agent_id: AgentId) -> dict[str, Any]:
        """List an agent's configuration history, newest first.

        Each entry is the configuration as it was *before* the change that superseded it. Pass a
        version number to rollback_agent to restore one.
        """
        async with workspace(McpScope.READ) as ws:
            versions = await ws.agents.list_versions(agent_id)
            return {"items": [render.agent_version(snapshot) for snapshot in versions]}

    @server.tool(title="Create agent", annotations=CREATES)
    async def create_agent(
        name: Name,
        persona: Persona = "",
        engagement_rules: Rules = None,
        guardrails: Rails = None,
        model_provider: Provider = None,
        model_settings: Settings = None,
    ) -> dict[str, Any]:
        """Create an agent as a draft.

        Configuration may be incomplete for now — a persona, provider and model are only required
        when publishing. The agent's own provider API key cannot be set here: ask the user to add
        it in the web console, or the agent uses the deployment's key for that provider.
        """
        async with workspace(McpScope.WRITE) as ws:
            created = await ws.agents.create(
                name=name,
                persona=persona,
                engagement_rules=(engagement_rules or EngagementRules()).model_dump(),
                guardrails=(guardrails or Guardrails()).model_dump(),
                model_provider=model_provider,
                model_settings=model_settings.model_dump() if model_settings else None,
            )
            return render.agent(created)

    @server.tool(title="Update agent", annotations=UPDATES)
    async def update_agent(
        agent_id: AgentId,
        name: Annotated[
            str | None, Field(min_length=1, max_length=255, description="A new display name.")
        ] = None,
        persona: Annotated[str | None, Field(max_length=8000, description="A new persona.")] = None,
        engagement_rules: Rules = None,
        guardrails: Rails = None,
        model_provider: Provider = None,
        model_settings: Settings = None,
        note: Annotated[
            str | None,
            Field(max_length=255, description="Why the change was made, kept on the version."),
        ] = None,
    ) -> dict[str, Any]:
        """Change an agent's configuration. Omitted fields are left as they are.

        Each object you pass (engagement_rules, guardrails, model_settings) **replaces** the stored
        one — read it with get_agent first and send the whole object back. The previous
        configuration is snapshotted and the version increments, so any change can be undone with
        rollback_agent. A published agent serves the new configuration immediately.
        """
        async with workspace(McpScope.WRITE) as ws:
            changes: dict[str, object] = {
                "name": name,
                "persona": persona,
                "engagement_rules": engagement_rules.model_dump() if engagement_rules else None,
                "guardrails": guardrails.model_dump() if guardrails else None,
                "model_provider": model_provider,
                "model_config_json": model_settings.model_dump() if model_settings else None,
            }
            return render.agent(await ws.agents.update(agent_id, changes, note=note))

    @server.tool(title="Set agent status", annotations=UPDATES)
    async def set_agent_status(
        agent_id: AgentId,
        action: Annotated[
            Literal["publish", "pause", "unpublish"],
            Field(
                description=(
                    "`publish` starts serving traffic on every channel (it needs a persona, a "
                    "provider and a model). `pause` stops a published agent while keeping its "
                    "integrations. `unpublish` returns it to draft."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Publish, pause, or unpublish an agent.

        Only a published agent answers real customers. Confirm with the user before pausing or
        unpublishing an agent that is live.
        """
        async with workspace(McpScope.WRITE) as ws:
            transition = {
                "publish": ws.agents.publish,
                "pause": ws.agents.pause,
                "unpublish": ws.agents.unpublish,
            }[action]
            return render.agent(await transition(agent_id))

    @server.tool(title="Roll back agent", annotations=UPDATES)
    async def rollback_agent(
        agent_id: AgentId,
        version: Annotated[
            int, Field(ge=1, description="Version number from list_agent_versions.")
        ],
        note: Annotated[
            str | None, Field(max_length=255, description="Why the rollback was made.")
        ] = None,
    ) -> dict[str, Any]:
        """Restore an earlier configuration.

        History is never rewritten: the current configuration is snapshotted first and the restore
        lands as a new version, so a rollback can itself be rolled back. Status is unchanged.
        """
        async with workspace(McpScope.WRITE) as ws:
            return render.agent(await ws.agents.rollback(agent_id, version, note=note))
