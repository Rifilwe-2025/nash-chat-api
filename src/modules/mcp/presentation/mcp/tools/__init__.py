"""Every group of MCP tools, each registering itself on the server.

Grouped by the module whose services the tools call, so a tool lives beside its neighbours and the
list of what a coding agent can do reads module by module.
"""

from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer

from src.modules.mcp.presentation.mcp.tools import (
    account,
    agent_tools,
    agents,
    analytics,
    channels,
    conversations,
    knowledge,
)

REGISTRARS: tuple[Callable[[MCPServer[Any]], None], ...] = (
    account.register,
    agents.register,
    knowledge.register,
    agent_tools.register,
    conversations.register,
    channels.register,
    analytics.register,
)
