"""drop usage_counter

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-30 17:22:51.400913
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Remove the metering table along with the billing module.

    The platform is not charging for use, so plan ceilings and per-period counters have no reader.
    What usage reporting remains lives in ``analytics``, which counts the same traffic from the
    message rows themselves — the difference being that those figures move when a tenant deletes a
    conversation, which only ever mattered because an invoice must not.

    A forward migration rather than an edit to 0013: that migration has been applied, and rewriting
    applied history would leave any database that ran it disagreeing with the file that claims to
    describe it.
    """
    op.drop_index("ix_usage_counter_tenant_period", table_name="usage_counter")
    op.drop_index(op.f("ix_usage_counter_tenant_id"), table_name="usage_counter")
    op.drop_table("usage_counter")


def downgrade() -> None:
    """Recreate the table, empty.

    Counters cannot be reconstructed — they were a running total, not a view over rows that still
    exist — so a downgrade restores the shape and not the numbers.
    """
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
        sa.UniqueConstraint(
            "tenant_id", "period", "metric", name="uq_usage_counter_tenant_id_period_metric"
        ),
    )
    op.create_index(op.f("ix_usage_counter_tenant_id"), "usage_counter", ["tenant_id"])
    op.create_index("ix_usage_counter_tenant_period", "usage_counter", ["tenant_id", "period"])
