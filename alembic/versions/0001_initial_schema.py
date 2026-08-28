"""initial schema: tenant and user

Creates the isolation boundary every later table hangs off. Enums are stored as constrained
varchars (``native_enum=False``) so adding a value later is a check-constraint change rather than a
Postgres type migration.

Revision ID: 0001
Revises:
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "plan",
            sa.Enum("FREE", "STARTER", "PRO", name="tenant_plan", native_enum=False, length=32),
            server_default="free",
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant")),
    )
    op.create_table(
        "user",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column(
            "role",
            sa.Enum("OWNER", "MEMBER", name="user_role", native_enum=False, length=32),
            server_default="owner",
            nullable=False,
        ),
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
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_user_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user")),
        sa.UniqueConstraint("email", name="uq_user_email"),
    )
    op.create_index(op.f("ix_user_tenant_id"), "user", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_user_tenant_id"), table_name="user")
    op.drop_table("user")
    op.drop_table("tenant")
