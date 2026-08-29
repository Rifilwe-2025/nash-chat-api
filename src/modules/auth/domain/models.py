"""Issued-token records.

Only the SHA-256 hash of a token is stored, never the token itself — a database leak must not hand
an attacker usable credentials. Rows are kept after expiry/revocation until
:mod:`src.modules.auth.internal.token_cleanup` prunes them, so a rejected token can be explained.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base_model import BaseModel, enum_column


class TokenType(str, enum.Enum):
    ACCESS = "access"
    REFRESH = "refresh"


class Token(BaseModel):
    __tablename__ = "token"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    token_type: Mapped[TokenType] = mapped_column(
        enum_column(TokenType, "token_type", length=16),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    def is_usable(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now
