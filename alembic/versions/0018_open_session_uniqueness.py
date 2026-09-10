"""open session uniqueness

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-10 10:05:12.418331
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow one *active* conversation per (agent, channel, external user).

    Until now a session was found and then created in two statements, so two first messages racing
    each other could each open a conversation. Any such duplicates must be resolved before the index
    can exist, and the resolution matches what callers already saw: the session lookup has always
    picked the newest active conversation, so the older duplicates were already unreachable. They
    are closed — not deleted, so their messages stay readable in the console.
    """
    op.execute(
        """
        UPDATE conversation AS older
        SET status = 'closed', updated_at = now()
        WHERE older.status = 'active'
          AND EXISTS (
              SELECT 1
              FROM conversation AS newer
              WHERE newer.status = 'active'
                AND newer.agent_id = older.agent_id
                AND newer.channel = older.channel
                AND newer.external_user_id = older.external_user_id
                AND (newer.created_at, newer.id) > (older.created_at, older.id)
          )
        """
    )
    op.create_index(
        "uq_conversation_open_session",
        "conversation",
        ["agent_id", "channel", "external_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_conversation_open_session", table_name="conversation")
