"""Personal access tokens — how a developer's own coding agent reaches the platform over MCP.

A third credential beside the two the API already has, and deliberately unlike either of them:

* **A user access token** lives fifteen minutes and every sign-in revokes it. That is right for a
  browser tab and wrong for an editor that stays connected for a working day — the agent would be
  signed out every time its owner opened the console.
* **An agent API key** speaks for one published agent and can only chat. A coding agent that builds
  and configures agents needs the builder's reach, not a website widget's.

A personal access token speaks for **a user, inside that user's own tenant**, with the scopes
and the lifetime the user chose when issuing it. It is authenticated the way the other two are — by
a SHA-256 hash — and it lives in a table of its own so that nothing that ends a browser session (a
sign-in, a refresh, a logout) can reach it by accident. Revoking one is a column rather than a
delete, so a revoked token stays visible in the owner's list and its use stays attributable;
deleting one is the owner's explicit choice to drop that record as well.

``encrypted_secret`` is what lets the owner copy a token again later, and it is optional by design —
see :mod:`src.shared.crypto.copies`.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base_model import TenantScopedModel


class McpScope(str, enum.Enum):
    """What a token may do through MCP.

    Two capabilities, because the line that matters to a person handing a credential to a coding
    agent is whether it can *change* anything. ``WRITE`` includes ``READ``: an agent allowed to edit
    a configuration it cannot read back would be editing blind.
    """

    READ = "mcp:read"
    WRITE = "mcp:write"


#: Least privilege by default — a token that can change things is one somebody asked for.
DEFAULT_SCOPES: tuple[str, ...] = (McpScope.READ.value,)


class PersonalAccessToken(TenantScopedModel):
    """One long-lived credential for one user's coding agent."""

    __tablename__ = "personal_access_token"
    # Every read a user makes of their tokens is "mine, in my tenant"; the unique index on the hash
    # serves authentication and nothing else.
    __table_args__ = (Index("ix_personal_access_token_tenant_user", "tenant_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # The opening characters, in clear, so two tokens can be told apart in a list without either
    # being recoverable.
    prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    # The secret under AES-256-GCM, for the owner to copy again. Null when no encryption key was
    # configured at issue, and cleared on revoke. Authentication never reads it.
    encrypted_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
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
        """A copy was kept, and the token would still work if it were pasted somewhere."""
        return self.encrypted_secret is not None and self.is_active

    def allows(self, scope: McpScope) -> bool:
        granted = {str(value) for value in (self.scopes or [])}
        if scope is McpScope.READ and McpScope.WRITE.value in granted:
            return True
        return scope.value in granted
