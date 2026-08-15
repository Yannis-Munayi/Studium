"""add updated_at to session_summaries and retrieval_checks

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-15

Spec v1.1 §6.11, finding A3. Both tables describe themselves as regenerable --
a summary can be rebuilt after a prompt improvement, a retrieval check after a
scoring fix -- but neither carried the timestamp that regenerability implies.
Without it there is no way to tell a first generation from a fifth, and §14's
"every mutable table has an updated_at trigger" check has nothing to assert.

Additive: two nullable-then-defaulted columns and two triggers, no downtime.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TABLES = ("session_summaries", "retrieval_checks")


def _updated_at_column() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    )


def upgrade() -> None:
    op.add_column("session_summaries", _updated_at_column())
    op.add_column("retrieval_checks", _updated_at_column())

    # Written out rather than looped: the trigger names have to be greppable in
    # the migration source, both for hand review (§13) and for the §14 check
    # that every mutable table gets one.
    op.execute(
        "CREATE TRIGGER trg_session_summaries_updated_at "
        "BEFORE UPDATE ON session_summaries "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_retrieval_checks_updated_at "
        "BEFORE UPDATE ON retrieval_checks "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_retrieval_checks_updated_at ON retrieval_checks"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_session_summaries_updated_at ON session_summaries"
    )
    op.drop_column("retrieval_checks", "updated_at")
    op.drop_column("session_summaries", "updated_at")
