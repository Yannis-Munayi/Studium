"""seed the system synthetic user

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-15

Spec v1.1 §6.12 attributes unattributable cost -- Curator pre-generation,
corpus imports -- to "the system synthetic user", but never defines one.

It cannot simply be booked to a NULL owner, because §6.12 also reserves that:
"Rows with user_id IS NULL represent post-erasure aggregates; the daily job
never inserts a NULL owner directly." Overloading NULL would make an erased
learner's aggregate indistinguishable from shared infrastructure cost, which
defeats both readings.

So the account exists as a real row with a fixed, recognisable id. It has no
profile, no enrollments and no sessions; it only ever appears as the owner of
ledger rows. Budget caps are seeded generously because the system account is
not subject to a per-learner ceiling -- and because ``budget_status`` raises
rather than guesses when a caps row is missing.

A data migration, kept separate from the schema change in 0007 per §13 so it
can be re-run on failure without re-applying the DDL.
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

SYSTEM_USER_ID = "00000000-0000-7000-8000-000000000001"
SYSTEM_USER_EMAIL = "system@studium.internal"


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO users (id, email, display_name, role, auth_provider, password_hash)
        VALUES (
            '{SYSTEM_USER_ID}',
            '{SYSTEM_USER_EMAIL}',
            'System (automated)',
            'admin',
            'local',
            NULL
        )
        ON CONFLICT (id) DO NOTHING
        """
    )
    # No password_hash and no auth_sessions: the account cannot be logged into.
    op.execute(
        f"""
        INSERT INTO user_budget_caps (
            user_id, daily_soft_usd, daily_hard_usd,
            monthly_soft_usd, monthly_hard_usd
        )
        VALUES ('{SYSTEM_USER_ID}', 1000.00, 2000.00, 20000.00, 40000.00)
        ON CONFLICT (user_id) DO NOTHING
        """
    )


def downgrade() -> None:
    # Ledger rows reference the account with ON DELETE SET NULL, so removing it
    # silently converts shared-infrastructure cost into what reads as
    # post-erasure aggregate. Detach explicitly first so the downgrade is at
    # least legible in the data.
    op.execute(
        f"UPDATE cost_ledger SET model = 'orphaned:' || model "
        f"WHERE user_id = '{SYSTEM_USER_ID}'"
    )
    op.execute(f"DELETE FROM user_budget_caps WHERE user_id = '{SYSTEM_USER_ID}'")
    op.execute(f"DELETE FROM users WHERE id = '{SYSTEM_USER_ID}'")
