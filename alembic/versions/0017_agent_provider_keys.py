"""agent provider keys

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-01 09:12:41.220118
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Give each agent somewhere to keep its own provider credential (spec §5.3, §9).

    Nullable, with no default and no backfill: every existing agent is currently served by whatever
    key the deployment configured, and null is exactly what "this agent has none of its own, use
    the platform's" means. Nobody's agent changes behaviour when this runs.

    Plain ``String`` here rather than the model's ``EncryptedString``: the encryption is a bind
    parameter concern, not a storage one — ciphertext is text — and a migration that imported the
    application's column types would break the moment those types moved.

    1024 characters is generous for a provider key (the longest in circulation are a few hundred)
    and has to be, because what is stored is the base64 envelope rather than the key itself.
    """
    op.add_column("agent", sa.Column("model_api_key", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    op.drop_column("agent", "model_api_key")
