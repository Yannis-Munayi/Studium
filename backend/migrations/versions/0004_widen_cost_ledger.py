"""widen cost_ledger to the five cost categories of spec v1.1

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-15

Spec v1.1 §6.12. The ledger now carries one column per cost source rather than
a single total, and ``cost_usd`` becomes a generated column summing them. The
reasoning the spec gives is the one that matters: v1.0 summed only
``agent_traces``, so content generation, ingestion, summarisation and grading
never reached the number budget enforcement reads -- and at MVP those are
plausibly the larger share, because content is generated once and read many
times.

Cache-write tokens split by TTL for the same class of reason: a 5-minute write
bills at 1.25x base input and a 1-hour write at 2x, so a single column cannot
re-derive cost once both are in use.

Non-additive: ``cache_write_tokens`` and the plain ``cost_usd`` column are
replaced. Per §13 that needs a maintenance window rather than a rolling deploy;
at MVP scale with no production data that is a formality, and the downgrade
below restores the v1.0 shape by folding the categories back into one total.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

CATEGORIES = (
    ("cost_agent_usd", "agent_traces"),
    ("cost_content_usd", "content_artifacts"),
    ("cost_ingestion_usd", "ingestion_jobs"),
    ("cost_summary_usd", "session_summaries"),
    ("cost_grading_usd", "assessment_attempts.grading_cost_usd"),
)

GENERATED_TOTAL = (
    "cost_agent_usd + cost_content_usd + cost_ingestion_usd"
    " + cost_summary_usd + cost_grading_usd"
)


def upgrade() -> None:
    # --- cache-write tokens, split by TTL ---------------------------------
    op.add_column(
        "cost_ledger",
        sa.Column(
            "cache_write_5m_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "cost_ledger",
        sa.Column(
            "cache_write_1h_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    # Existing rows cannot be attributed to a TTL after the fact. Booking them
    # to the 5-minute column is the conservative read: it is the default TTL,
    # and it under-states cost rather than over-stating it.
    op.execute(
        "UPDATE cost_ledger SET cache_write_5m_tokens = cache_write_tokens"
    )
    op.drop_column("cost_ledger", "cache_write_tokens")

    # --- five cost categories ---------------------------------------------
    for column, _source in CATEGORIES:
        op.add_column(
            "cost_ledger",
            sa.Column(
                column,
                sa.Numeric(12, 4),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    # The pre-existing total was agent-trace cost only, so that is where the
    # historical value belongs.
    op.execute("UPDATE cost_ledger SET cost_agent_usd = cost_usd")
    op.drop_column("cost_ledger", "cost_usd")
    op.add_column(
        "cost_ledger",
        sa.Column(
            "cost_usd",
            sa.Numeric(12, 4),
            sa.Computed(GENERATED_TOTAL, persisted=True),
            nullable=True,
        ),
    )

    # --- narrow the owner index to non-anonymised rows ---------------------
    # After erasure a ledger row keeps its aggregate with user_id NULL. Those
    # rows are never fetched by owner, so they do not belong in this index.
    op.drop_index("idx_cost_ledger_user_day", table_name="cost_ledger")
    op.create_index(
        "idx_cost_ledger_user_day",
        "cost_ledger",
        ["user_id", sa.text("day DESC")],
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_cost_ledger_user_day", table_name="cost_ledger")
    op.create_index(
        "idx_cost_ledger_user_day",
        "cost_ledger",
        ["user_id", sa.text("day DESC")],
    )

    # Fold the categories back into a single writable total before dropping
    # them, so no cost is lost on the way down.
    op.drop_column("cost_ledger", "cost_usd")
    op.add_column(
        "cost_ledger",
        sa.Column(
            "cost_usd",
            sa.Numeric(12, 4),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.execute(f"UPDATE cost_ledger SET cost_usd = {GENERATED_TOTAL}")
    for column, _source in CATEGORIES:
        op.drop_column("cost_ledger", column)

    op.add_column(
        "cost_ledger",
        sa.Column(
            "cache_write_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.execute(
        "UPDATE cost_ledger SET cache_write_tokens = "
        "cache_write_5m_tokens + cache_write_1h_tokens"
    )
    op.drop_column("cost_ledger", "cache_write_1h_tokens")
    op.drop_column("cost_ledger", "cache_write_5m_tokens")
