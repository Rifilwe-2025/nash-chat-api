"""Agent configuration and its version history (spec §5.1, §7).

The parts of an agent that shape its behaviour — persona, engagement rules, guardrails, model
settings — are stored as JSON rather than columns. They are read and written as a whole by the
builder UI, the shapes will keep moving through v1, and nothing queries *inside* them; making each
field a column would mean a migration per product tweak with no payoff.

``agent_version`` keeps a snapshot of the configuration before each change, which is what makes
rollback possible. Snapshots are immutable — rolling back writes a *new* version rather than
deleting history.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import Enum as SqlEnum
from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.database.base_model import BaseModel, TenantScopedModel


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
        SqlEnum(ModelProvider, name="model_provider", native_enum=False, length=32),
        nullable=True,
    )
    model_config_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    status: Mapped[AgentStatus] = mapped_column(
        SqlEnum(AgentStatus, name="agent_status", native_enum=False, length=32),
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
