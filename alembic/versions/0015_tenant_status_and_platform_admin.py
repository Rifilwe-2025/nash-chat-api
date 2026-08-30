"""tenant status and platform admin

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-30 18:07:33.284915
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Every existing account is active. The default is on the column as well as here, so a tenant
    # created by code that predates this migration is enabled rather than null.
    op.add_column(
        "tenant",
        sa.Column(
            "status",
            sa.Enum("active", "disabled", name="tenant_status", native_enum=False, length=32),
            server_default="active",
            nullable=False,
        ),
    )
    op.add_column("tenant", sa.Column("status_note", sa.String(length=500), nullable=True))
    op.add_column(
        "tenant", sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True)
    )
    # The admin console filters on it, and the authentication path reads it on every request.
    op.create_index(op.f("ix_tenant_status"), "tenant", ["status"])

    # Nobody is platform staff until somebody is granted it out of band, by
    # scripts/grant_platform_admin.py. There is no API that sets this.
    op.add_column(
        "user",
        sa.Column("is_platform_admin", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("user", "is_platform_admin")
    op.drop_index(op.f("ix_tenant_status"), table_name="tenant")
    op.drop_column("tenant", "status_changed_at")
    op.drop_column("tenant", "status_note")
    op.drop_column("tenant", "status")
