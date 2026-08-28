"""Agent configuration schema — the actual product surface (spec §5.1).

The JSON columns on the model are unstructured at the database level, but everything entering them
passes through these models first, so the stored shape is always validated and documented. Swagger
renders this as the contract the builder UI codes against.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from src.modules.agents.domain.models import AgentStatus, ModelProvider
from src.shared.responses import CamelModel

MAX_RULES = 50


class EngagementRules(CamelModel):
    """How the agent talks (spec §5.1: tone, engagement style, do's and don'ts)."""

    tone: str | None = Field(
        default=None,
        max_length=255,
        description="Voice the agent should adopt.",
        examples=["Warm and concise", "Formal and precise"],
    )
    style: str | None = Field(
        default=None,
        max_length=1000,
        description="How it should engage — question handling, greetings, length of replies.",
    )
    dos: list[str] = Field(
        default_factory=list,
        max_length=MAX_RULES,
        description="Behaviours the agent should follow.",
        examples=[["Offer the colour-matching service", "Confirm quantities before quoting"]],
    )
    donts: list[str] = Field(
        default_factory=list,
        max_length=MAX_RULES,
        description="Behaviours the agent must avoid.",
        examples=[["Never promise a delivery date", "Never discuss competitor pricing"]],
    )
    escalation_triggers: list[str] = Field(
        default_factory=list,
        max_length=MAX_RULES,
        description="Situations that should hand the conversation to a human.",
        examples=[["Customer asks for a refund", "Customer is angry"]],
    )


class Guardrails(CamelModel):
    """What the agent must not do (spec §5.1: restricted topics, moderation, fallbacks)."""

    restricted_topics: list[str] = Field(
        default_factory=list,
        max_length=MAX_RULES,
        description="Subjects the agent must decline to discuss.",
        examples=[["Legal advice", "Medical advice"]],
    )
    fallback_response: str | None = Field(
        default=None,
        max_length=2000,
        description="Reply used when the agent cannot answer from its knowledge.",
        examples=["I don't have that information — let me connect you with the team."],
    )
    require_grounded_answers: bool = Field(
        default=True,
        description=(
            "When true the agent must answer only from its knowledge base and decline otherwise, "
            "rather than relying on the model's general knowledge."
        ),
    )


class ModelSettings(CamelModel):
    """Which brain runs the agent (spec §5.3). Swapping provider is config, never code."""

    model: str = Field(
        min_length=1,
        max_length=128,
        description="Provider-specific model identifier.",
        examples=["gemini-2.0-flash", "gpt-4o", "claude-sonnet-4-5"],
    )
    temperature: float = Field(
        default=0.7, ge=0.0, le=2.0, description="Sampling temperature.", examples=[0.7]
    )
    max_tokens: int = Field(
        default=1024, ge=1, le=32000, description="Cap on generated tokens.", examples=[1024]
    )


class AgentConfig(CamelModel):
    """The versioned part of an agent — everything a rollback restores."""

    persona: str = Field(
        default="",
        max_length=8000,
        description="Character description: who the agent is and what it is there to do.",
        examples=["You are the sales assistant for Nash Paints, a Zimbabwean paint retailer."],
    )
    engagement_rules: EngagementRules = Field(default_factory=EngagementRules)
    guardrails: Guardrails = Field(default_factory=Guardrails)
    model_provider: ModelProvider | None = Field(
        default=None, description="LLM provider. Required before the agent can be published."
    )
    model_settings: ModelSettings | None = Field(
        default=None, description="Model choice and sampling. Required before publishing."
    )


class CreateAgentRequest(CamelModel):
    name: str = Field(
        min_length=1,
        max_length=255,
        description="Display name, unique within your tenant.",
        examples=["Sales Assistant"],
    )
    persona: str = Field(default="", max_length=8000, description="Character description.")
    engagement_rules: EngagementRules = Field(default_factory=EngagementRules)
    guardrails: Guardrails = Field(default_factory=Guardrails)
    model_provider: ModelProvider | None = None
    model_settings: ModelSettings | None = None


class UpdateAgentRequest(CamelModel):
    """Every field is optional — omitted fields are left unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    persona: str | None = Field(default=None, max_length=8000)
    engagement_rules: EngagementRules | None = None
    guardrails: Guardrails | None = None
    model_provider: ModelProvider | None = None
    model_settings: ModelSettings | None = None


class RollbackRequest(CamelModel):
    note: str | None = Field(
        default=None, max_length=255, description="Optional reason recorded on the new version."
    )


class AgentResponse(CamelModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    status: AgentStatus = Field(description="Lifecycle state.", examples=["draft"])
    version: int = Field(description="Increments on every configuration change.", examples=[3])
    persona: str
    engagement_rules: EngagementRules
    guardrails: Guardrails
    model_provider: ModelProvider | None
    model_settings: ModelSettings | None
    created_at: datetime
    updated_at: datetime


class AgentSummaryResponse(CamelModel):
    """Trimmed shape for list endpoints — full configuration is fetched per agent."""

    id: uuid.UUID
    name: str
    status: AgentStatus
    version: int
    model_provider: ModelProvider | None
    updated_at: datetime


class AgentVersionResponse(CamelModel):
    version: int
    note: str | None
    config: dict[str, Any] = Field(description="The configuration captured in this snapshot.")
    created_at: datetime
