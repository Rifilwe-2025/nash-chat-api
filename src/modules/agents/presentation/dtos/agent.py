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
from src.shared.llm.verification import KeyCheckStatus
from src.shared.responses import CamelModel

MAX_RULES = 50
MAX_API_KEY = 1024

#: How much of a stored key is echoed back so a person can recognise which one it is. Four
#: characters identifies a key to whoever pasted it and is useless to anybody else.
HINT_CHARACTERS = 4


def key_hint(api_key: str | None) -> str | None:
    """The tail of a stored key, as ``…abcd``. ``None`` when nothing is stored."""
    if not api_key:
        return None
    return f"…{api_key[-HINT_CHARACTERS:]}" if len(api_key) > HINT_CHARACTERS else "…"


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


API_KEY_FIELD_DESCRIPTION = (
    "Your own API key for the selected provider, stored encrypted and used for this agent's "
    "requests. Never returned; the response reports whether one is set and shows its last four "
    "characters. Omit it to leave the stored key alone."
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
    model_api_key: str | None = Field(
        default=None,
        max_length=MAX_API_KEY,
        description=API_KEY_FIELD_DESCRIPTION,
        examples=["AIzaSy..."],
    )


class UpdateAgentRequest(CamelModel):
    """Every field is optional — omitted fields are left unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    persona: str | None = Field(default=None, max_length=8000)
    engagement_rules: EngagementRules | None = None
    guardrails: Guardrails | None = None
    model_provider: ModelProvider | None = None
    model_settings: ModelSettings | None = None
    model_api_key: str | None = Field(
        default=None,
        max_length=MAX_API_KEY,
        description=(
            API_KEY_FIELD_DESCRIPTION
            + " Replacing it does not create a version — credentials are not part of the "
            "configuration history. Send `DELETE /agents/{id}/model-key` to remove one."
        ),
    )


class ModelKeyTestRequest(CamelModel):
    """A key to try, and what to try it against.

    Every field is optional so one endpoint serves both moments it is needed: testing a key that is
    already stored (send nothing), and testing one that has been typed into the builder but not
    saved yet (send the key). Testing before saving is the point — otherwise the only way to find
    out a credential is wrong is to store it and wait for a customer to hit the error.
    """

    model_api_key: str | None = Field(
        default=None,
        max_length=MAX_API_KEY,
        description="Key to test. Omit to test the one already stored on the agent.",
    )
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Model to test against. Omit to use the agent's configured model.",
        examples=["gemini-2.0-flash"],
    )


class ProviderKeyTestRequest(CamelModel):
    """A standalone check, for an agent that does not exist yet."""

    model_provider: ModelProvider = Field(description="Provider the key belongs to.")
    model: str = Field(
        min_length=1,
        max_length=128,
        description="Model the key should be able to reach.",
        examples=["gemini-2.0-flash"],
    )
    model_api_key: str = Field(
        min_length=1, max_length=MAX_API_KEY, description="The key to test. Never stored."
    )


class ModelKeyTestResponse(CamelModel):
    """What the provider said. A rejected key is a 200 with `ok: false`, not an error.

    The request succeeded — the platform asked a question and got an answer. Reporting "your key is
    wrong" as a 4xx would make it indistinguishable, to a client's error handling, from "your
    request to test it was malformed".
    """

    ok: bool = Field(description="True only when the provider answered successfully.")
    status: KeyCheckStatus = Field(
        description=(
            "Why it failed, in terms you can act on: `invalid_key` (the provider rejected the "
            "credential), `model_rejected` (the key works, the model name or its access does "
            "not), `rate_limited` (the key works and is throttled right now), `unavailable` (the "
            "provider could not be reached — this says nothing about the key), `not_configured` "
            "(no key was supplied and none is stored)."
        ),
        examples=["ok"],
    )
    provider: ModelProvider = Field(description="Provider that was contacted.")
    model: str = Field(description="Model the probe ran against.")
    latency_ms: int = Field(description="Round trip to the provider, in milliseconds.")
    detail: str = Field(
        description="The provider's own message, or guidance when it did not give one."
    )


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
    has_model_api_key: bool = Field(
        description=(
            "Whether this agent has its own provider key stored. False means it falls back to "
            "whatever key the deployment is configured with, which may be none."
        ),
        examples=[True],
    )
    model_api_key_hint: str | None = Field(
        default=None,
        description="Last four characters of the stored key, so it can be told apart from another.",
        examples=["…a91f"],
    )
    created_at: datetime
    updated_at: datetime


class AgentSummaryResponse(CamelModel):
    """Trimmed shape for list endpoints — full configuration is fetched per agent."""

    id: uuid.UUID
    name: str
    status: AgentStatus
    version: int
    model_provider: ModelProvider | None
    has_model_api_key: bool = Field(
        description="Whether the agent carries its own provider key.", examples=[True]
    )
    updated_at: datetime


class AgentVersionResponse(CamelModel):
    version: int
    note: str | None
    config: dict[str, Any] = Field(description="The configuration captured in this snapshot.")
    created_at: datetime
