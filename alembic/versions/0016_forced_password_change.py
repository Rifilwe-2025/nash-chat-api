"""forced password change

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-30 20:48:12.663704
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Mark accounts whose password was chosen by somebody other than their owner.

    Every existing account chose its own password at sign-up, so the default is false and nobody is
    locked out by this migration. The only account that starts with it set is the platform
    administrator a deployment creates from its environment, whose first password is readable by
    everyone who can read that environment.
    """
    op.add_column(
        "user",
        sa.Column("must_change_password", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("user", "must_change_password")
