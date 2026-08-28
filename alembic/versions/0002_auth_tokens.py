"""auth tokens

Records every issued token by SHA-256 hash so revocation is real: logout and refresh-rotation mark
rows revoked, and a token whose row is missing, revoked, or expired is rejected even when its
signature still verifies. The hash is unique — two tokens can never collide onto one row.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "token",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "token_type",
            sa.Enum("ACCESS", "REFRESH", name="token_type", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_token_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_token")),
    )
    op.create_index(op.f("ix_token_token_hash"), "token", ["token_hash"], unique=True)
    op.create_index(op.f("ix_token_user_id"), "token", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_token_user_id"), table_name="token")
    op.drop_index(op.f("ix_token_token_hash"), table_name="token")
    op.drop_table("token")
