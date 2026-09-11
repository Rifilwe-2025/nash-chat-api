"""API keys (spec §5.6, §7).

**A key is authenticated by its hash.** Only a SHA-256 hash is looked up, exactly as auth tokens
are handled — a key is a high-entropy random secret, not a human-chosen password, so it needs no
work factor but must be found by exact value.

``encrypted_secret`` is a separate, optional copy so a tenant can copy a key again from the console.
It is encrypted with ``SECURITY_ENCRYPTION_KEY``, absent when none is configured, and discarded on
revoke (see :mod:`src.shared.crypto.copies`). Without it, a lost key is replaced, not recovered.

``prefix`` exists so a key is identifiable without being recoverable. It is the opening characters,
stored in clear, which is what lets a console show ``nsk_live_7Fq2…`` beside a key's name so a
tenant can tell two keys apart when deciding which to revoke.

``scopes`` is a list rather than a role: a website widget sends messages and reads its own thread,
while a reporting job reads conversations and should never be able to write one. Those are separate
capabilities from the start rather than a permission model bolted on later.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base_model import TenantScopedModel


class ApiKeyScope(str, enum.Enum):
    """What a key may do. Small on purpose — each value is a capability worth having alone."""

    CHAT_WRITE = "chat:write"
    CHAT_READ = "chat:read"


DEFAULT_SCOPES: tuple[str, ...] = (ApiKeyScope.CHAT_WRITE.value, ApiKeyScope.CHAT_READ.value)


class ApiKey(TenantScopedModel):
    """One integration credential, bound to a single agent (spec §7)."""

    __tablename__ = "api_key"
    # A tenant's key list is filtered by agent; the unique index on the hash serves authentication
    # and nothing else.
    __table_args__ = (Index("ix_api_key_tenant_agent", "tenant_id", "agent_id"),)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Unique so a lookup by hash is an index hit and a collision is impossible by construction.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    # The secret under AES-256-GCM, for the tenant to copy again. Null when no encryption key was
    # configured at issue, and cleared on revoke. Authentication never reads it.
    encrypted_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.now(UTC)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and not self.is_expired

    @property
    def is_copyable(self) -> bool:
        """A copy was kept, and the key would still work if it were pasted somewhere."""
        return self.encrypted_secret is not None and self.is_active

    def allows(self, scope: ApiKeyScope) -> bool:
        return scope.value in (self.scopes or [])
