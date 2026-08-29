"""retrieval tiers

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-29 09:12:44.118203
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every varchar-backed enum in the schema. Each member's value is its lowercased name, so one
# `lower()` migrates them all — see the note in `upgrade`.
ENUM_COLUMNS: tuple[tuple[str, str], ...] = (
    ("tenant", "plan"),
    ("user", "role"),
    ("token", "token_type"),
    ("agent", "status"),
    ("agent", "model_provider"),
    ("knowledge_base", "retrieval_tier"),
    ("kb_source", "type"),
    ("kb_source", "status"),
)

SEARCH_VECTOR = (
    "setweight(to_tsvector('english', coalesce(name, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(extracted_text, '')), 'B')"
)


def upgrade() -> None:
    # -- enum storage: names become values -------------------------------------
    #
    # SQLAlchemy persisted `.name` ("DRAFT") while every `server_default` here — and every JSON
    # response — used `.value` ("draft"). Nothing had broken yet only because the ORM always
    # supplies these columns; a row inserted without one would store the default and then fail to
    # load. The models now pin `values_callable`, so the existing rows are rewritten to match.
    for table, column in ENUM_COLUMNS:
        op.execute(f'UPDATE "{table}" SET {column} = lower({column}) WHERE {column} IS NOT NULL')

    # -- tier routing ----------------------------------------------------------
    #
    # `auto` becomes the default: the tier is picked per query from how much text the knowledge
    # base holds, so one that grows past the injection budget starts being searched on its own.
    # Existing knowledge bases keep whatever they were explicitly created with.
    op.alter_column("knowledge_base", "retrieval_tier", server_default="auto")

    # -- Tier 2 index ----------------------------------------------------------
    #
    # Generated rather than trigger-maintained: Postgres recomputes it whenever the text changes,
    # so the index cannot drift out of step with `extracted_text`.
    op.add_column(
        "kb_source",
        sa.Column(
            "search_vector",
            sa.dialects.postgresql.TSVECTOR(),
            sa.Computed(SEARCH_VECTOR, persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_kb_source_search_vector",
        "kb_source",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_kb_source_search_vector", table_name="kb_source", postgresql_using="gin")
    op.drop_column("kb_source", "search_vector")
    op.alter_column("knowledge_base", "retrieval_tier", server_default="direct")

    # `auto` has no equivalent in the previous schema; the closest honest downgrade is the tier it
    # would most often have resolved to for a knowledge base small enough to have been created one.
    op.execute("UPDATE knowledge_base SET retrieval_tier = 'direct' WHERE retrieval_tier = 'auto'")

    for table, column in ENUM_COLUMNS:
        op.execute(f'UPDATE "{table}" SET {column} = upper({column}) WHERE {column} IS NOT NULL')
