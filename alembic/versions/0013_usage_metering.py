"""usage metering

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-30 14:41:09.552310
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_counter",
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column(
            "metric",
            sa.Enum(
                "messages",
                "prompt_tokens",
                "completion_tokens",
                "cost_micro_usd",
                name="usage_metric",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("value", sa.BigInteger(), server_default="0", nullable=False),
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
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_usage_counter_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_counter")),
        # Named explicitly because the metering upsert targets it by name: one row per
        # (tenant, period, metric), so concurrent turns add to the same row rather than racing to
        # create two.
        sa.UniqueConstraint(
            "tenant_id", "period", "metric", name="uq_usage_counter_tenant_id_period_metric"
        ),
    )
    op.create_index(op.f("ix_usage_counter_tenant_id"), "usage_counter", ["tenant_id"])
    op.create_index("ix_usage_counter_tenant_period", "usage_counter", ["tenant_id", "period"])


def downgrade() -> None:
    op.drop_index("ix_usage_counter_tenant_period", table_name="usage_counter")
    op.drop_index(op.f("ix_usage_counter_tenant_id"), table_name="usage_counter")
    op.drop_table("usage_counter")
