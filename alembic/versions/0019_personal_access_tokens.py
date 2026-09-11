"""personal access tokens

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-10 14:20:37.105224
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """The credential a user issues for their own coding agent to reach the MCP endpoint.

    A table of its own rather than a new ``token_type`` on ``token``: issuing a sign-in pair revokes
    every row in that table for the user, and a long-lived editor credential must not be swept up
    by somebody opening the console.
    """
    op.create_table(
        "personal_access_token",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=24), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
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
            name=op.f("fk_personal_access_token_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_personal_access_token_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_personal_access_token")),
    )
    op.create_index(
        op.f("ix_personal_access_token_user_id"),
        "personal_access_token",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_personal_access_token_token_hash"),
        "personal_access_token",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_personal_access_token_tenant_id"),
        "personal_access_token",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_personal_access_token_tenant_user",
        "personal_access_token",
        ["tenant_id", "user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_personal_access_token_tenant_user", table_name="personal_access_token")
    op.drop_index(op.f("ix_personal_access_token_tenant_id"), table_name="personal_access_token")
    op.drop_index(op.f("ix_personal_access_token_token_hash"), table_name="personal_access_token")
    op.drop_index(op.f("ix_personal_access_token_user_id"), table_name="personal_access_token")
    op.drop_table("personal_access_token")
