"""Status transition rules (spec §5.1: draft / published / paused).

Kept apart from the service so the rules can be read — and tested — as a table rather than inferred
from a chain of conditionals.
"""

from __future__ import annotations

from src.modules.agents.domain.models import Agent, AgentStatus

ALLOWED: dict[AgentStatus, set[AgentStatus]] = {
    AgentStatus.DRAFT: {AgentStatus.PUBLISHED},
    AgentStatus.PUBLISHED: {AgentStatus.PAUSED, AgentStatus.DRAFT},
    AgentStatus.PAUSED: {AgentStatus.PUBLISHED, AgentStatus.DRAFT},
}


def can_transition(current: AgentStatus, target: AgentStatus) -> bool:
    return target in ALLOWED.get(current, set())


def publish_blockers(agent: Agent) -> list[str]:
    """What still has to be configured before this agent can serve traffic.

    Publishing is the point where an agent becomes reachable, so the checks live here rather than on
    every edit — a half-configured draft is a normal thing to save.
    """
    blockers: list[str] = []
    if not agent.persona.strip():
        blockers.append("persona is empty")
    if agent.model_provider is None:
        blockers.append("no model provider selected")
    if not agent.model_config_json.get("model"):
        blockers.append("no model selected")
    return blockers
