"""platform events

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-30 09:14:02.115377
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_event",
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column(
            "category",
            sa.Enum(
                "provider_error",
                name="platform_event_category",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column(
            "meta_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
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
            ["agent_id"],
            ["agent.id"],
            name=op.f("fk_platform_event_agent_id_agent"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_platform_event_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_event")),
    )
    op.create_index(op.f("ix_platform_event_agent_id"), "platform_event", ["agent_id"])
    op.create_index(op.f("ix_platform_event_tenant_id"), "platform_event", ["tenant_id"])
    op.create_index(
        "ix_platform_event_tenant_created", "platform_event", ["tenant_id", "created_at"]
    )
    op.create_index(
        "ix_platform_event_tenant_category",
        "platform_event",
        ["tenant_id", "category", "created_at"],
    )

    # Analytics reads messages by time, through their conversation. Without this the daily series
    # and every total is a sequential scan over the largest table in the schema.
    op.create_index("ix_message_conversation_created", "message", ["conversation_id", "created_at"])
    # The conversation counts filter on when a conversation started and when it was escalated.
    op.create_index("ix_conversation_tenant_created", "conversation", ["tenant_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_conversation_tenant_created", table_name="conversation")
    op.drop_index("ix_message_conversation_created", table_name="message")
    op.drop_index("ix_platform_event_tenant_category", table_name="platform_event")
    op.drop_index("ix_platform_event_tenant_created", table_name="platform_event")
    op.drop_index(op.f("ix_platform_event_tenant_id"), table_name="platform_event")
    op.drop_index(op.f("ix_platform_event_agent_id"), table_name="platform_event")
    op.drop_table("platform_event")
