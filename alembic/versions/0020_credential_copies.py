"""credential copies

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-11 09:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """An optional encrypted copy of each API key and personal access token.

    Nullable, and null for every existing row: those secrets were only ever hashed, so there is
    nothing to encrypt. They keep working and are simply not copyable.
    """
    op.add_column("api_key", sa.Column("encrypted_secret", sa.Text(), nullable=True))
    op.add_column("personal_access_token", sa.Column("encrypted_secret", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("personal_access_token", "encrypted_secret")
    op.drop_column("api_key", "encrypted_secret")
