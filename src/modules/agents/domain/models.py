"""Agent configuration and its version history (spec §5.1, §7).

The parts of an agent that shape its behaviour — persona, engagement rules, guardrails, model
settings — are stored as JSON rather than columns. They are read and written as a whole by the
builder UI, the shapes will keep moving through v1, and nothing queries *inside* them; making each
field a column would mean a migration per product tweak with no payoff.

``agent_version`` keeps a snapshot of the configuration before each change, which is what makes
rollback possible. Snapshots are immutable — rolling back writes a *new* version rather than
deleting history.

``model_api_key`` is the one part of the configuration that is **neither JSON nor versioned**,
and both exceptions are deliberate. It is a column of its own because it is encrypted at rest
(spec §5.7) and ``model_config_json`` is not — a secret inside a plain JSONB blob is a secret in
a database dump. It is left out of every snapshot for the same reason: ``agent_version.snapshot``
is readable JSONB that history never deletes, so putting a credential in it would keep every key
the tenant has ever pasted, in clear, forever. The practical consequence is that a rollback
restores an agent's behaviour and leaves its credential alone, which is also the behaviour
somebody rolling back a persona would expect.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.crypto import EncryptedString
from src.shared.database.base_model import BaseModel, TenantScopedModel, enum_column


class AgentStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    PAUSED = "paused"


class ModelProvider(str, enum.Enum):
    """Providers the LLM abstraction will implement in Phase 4 (spec §5.3)."""

    GEMINI = "gemini"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class Agent(TenantScopedModel):
    __tablename__ = "agent"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    persona: Mapped[str] = mapped_column(Text, nullable=False, default="")
    engagement_rules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    guardrails: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    model_provider: Mapped[ModelProvider | None] = mapped_column(
        enum_column(ModelProvider, "model_provider"),
        nullable=True,
    )
    model_config_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    #: The tenant's own key for ``model_provider``, encrypted at rest. ``None`` means the agent has
    #: none of its own and falls back to whatever key the deployment configured for that provider.
    model_api_key: Mapped[str | None] = mapped_column(EncryptedString(1024), nullable=True)
    status: Mapped[AgentStatus] = mapped_column(
        enum_column(AgentStatus, "agent_status"),
        nullable=False,
        default=AgentStatus.DRAFT,
        server_default=AgentStatus.DRAFT.value,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    versions: Mapped[list[AgentVersion]] = relationship(
        back_populates="agent",
        cascade="all, delete-orphan",
        order_by="AgentVersion.version",
    )


class AgentVersion(BaseModel):
    """An immutable snapshot of an agent's configuration at a point in time."""

    __tablename__ = "agent_version"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_version_agent_id_version"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    agent: Mapped[Agent] = relationship(back_populates="versions")
