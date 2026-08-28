"""Status transition table and publish preconditions, as unit tests."""

from __future__ import annotations

from typing import Any

import pytest

from src.modules.agents.domain.models import Agent, AgentStatus, ModelProvider
from src.modules.agents.internal.transitions import can_transition, publish_blockers


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (AgentStatus.DRAFT, AgentStatus.PUBLISHED, True),
        (AgentStatus.DRAFT, AgentStatus.PAUSED, False),
        (AgentStatus.PUBLISHED, AgentStatus.PAUSED, True),
        (AgentStatus.PUBLISHED, AgentStatus.DRAFT, True),
        (AgentStatus.PAUSED, AgentStatus.PUBLISHED, True),
        (AgentStatus.PAUSED, AgentStatus.DRAFT, True),
    ],
)
def test_transition_table(current: AgentStatus, target: AgentStatus, allowed: bool) -> None:
    assert can_transition(current, target) is allowed


def build(**overrides: Any) -> Agent:
    defaults: dict[str, Any] = {
        "name": "Agent",
        "persona": "You are helpful.",
        "engagement_rules": {},
        "guardrails": {},
        "model_provider": ModelProvider.OPENAI,
        "model_config_json": {"model": "gpt-4o"},
        "status": AgentStatus.DRAFT,
        "version": 1,
    }
    return Agent(**{**defaults, **overrides})


def test_a_fully_configured_agent_has_no_blockers() -> None:
    assert publish_blockers(build()) == []


def test_each_missing_piece_is_reported() -> None:
    assert publish_blockers(build(persona="   ")) == ["persona is empty"]
    assert publish_blockers(build(model_provider=None)) == ["no model provider selected"]
    assert publish_blockers(build(model_config_json={})) == ["no model selected"]


def test_blockers_accumulate() -> None:
    blockers = publish_blockers(build(persona="", model_provider=None, model_config_json={}))

    assert len(blockers) == 3
