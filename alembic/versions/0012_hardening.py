"""hardening: pii redaction flag, wider secret column, missing indexes

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-30 11:02:44.903118
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Opt-in PII redaction per knowledge base (spec §5.7). Defaults to off: redaction is lossy, and
    # turning it on for existing tenants would silently degrade agents that depend on the details.
    op.add_column(
        "knowledge_base",
        sa.Column("redact_pii", sa.Boolean(), server_default="false", nullable=False),
    )

    # Webhook secrets are now encrypted at rest, and a ciphertext is longer than its plaintext:
    # nonce, authentication tag, and base64. A column sized for the secret would begin truncating
    # on the day the key was turned on — which would look like a signature bug, not a schema one.
    op.alter_column(
        "webhook_endpoint",
        "secret",
        existing_type=sa.String(length=128),
        type_=sa.String(length=512),
        existing_nullable=False,
    )

    # Slow-query review (Phase 13). Each of these serves a read that runs on a request path and had
    # nothing better than a sequential scan.
    #
    # The API-key lookup is by hash and already unique-indexed; what was missing is the *listing* a
    # tenant sees, which filters by agent.
    op.create_index("ix_api_key_tenant_agent", "api_key", ["tenant_id", "agent_id"])
    # The source list and the storage total are both per knowledge base, newest first.
    op.create_index("ix_kb_source_kb_created", "kb_source", ["kb_id", "created_at"])
    # The failure report reads failed sources per tenant by when they failed.
    op.create_index("ix_kb_source_tenant_status", "kb_source", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_kb_source_tenant_status", table_name="kb_source")
    op.drop_index("ix_kb_source_kb_created", table_name="kb_source")
    op.drop_index("ix_api_key_tenant_agent", table_name="api_key")
    op.alter_column(
        "webhook_endpoint",
        "secret",
        existing_type=sa.String(length=512),
        type_=sa.String(length=128),
        existing_nullable=False,
    )
    op.drop_column("knowledge_base", "redact_pii")
