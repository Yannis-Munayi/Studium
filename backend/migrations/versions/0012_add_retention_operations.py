"""add retention operations and signing-key compromise tracking

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-26

Infrastructure spec (subsystem 7) §18 names three additions to the data layer
v1.2 batch, plus one implied by §12.2's audit needs. This applies all four:

**retention_actions** (§12.2) is the retention worker's audit trail: "why did
that data go away" is answered either by a row here or by "something else
happened, investigate". The column list is §12.2's DDL verbatim -- the spec
wrote it out in full, so departing from it would be a wider divergence than the
one thing it does not carry, which is the grouping of one nightly pass. That
lives in `metadata`, the fourth item §18 counts, alongside the policy window
and predicate that produced each row. See DIVERGENCES-INFRASTRUCTURE (N2).

Append-only, and this migration revokes UPDATE and DELETE on it from
`studium_app`. Migration 0003's ALTER DEFAULT PRIVILEGES grants both on future
tables, so a table added afterwards that belongs in APPEND_ONLY_TABLES has to
revoke for itself. An audit trail the audited process can rewrite answers
nothing.

**retention_holds** (§12.4) protects a specific row from the nightly worker --
evidence in a rights dispute, a research study, a longitudinal analysis.
Released rather than deleted when lifted: a hold that vanishes destroys the
only record that the data was deliberately kept, which is what the dispute
would later ask about. The partial unique index permits one live hold per
(table, row); a second `set-retention-hold` on the same row updates the reason
rather than stacking, because two holds with different reasons make "why is
this still here" ambiguous.

**signing_keys.compromised_at** (§11.4 step 2) records the *earliest possible*
compromise, not the moment it was noticed -- §11.4 step 3 publishes the window
"between the earliest possible compromise and rotation", and only the earlier
bound gets that window right. Advisory: a compromised key still verifies what
it signed, and invalidating every credential it ever issued would punish the
learners rather than the attacker. `credentials.verify_item` reports it and
lets the verifier decide. See DIVERGENCES-INFRASTRUCTURE (N4).

Additive throughout: two new tables, one new column, six new indexes, one new
trigger, one revoke. Existing rows are untouched.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

APP_ROLE = "studium_app"


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("uuid_generate_v7()"),
    )


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=None if nullable else sa.func.now(),
    )


def upgrade() -> None:
    # --- retention_actions (§12.2) -----------------------------------------
    op.create_table(
        "retention_actions",
        _uuid_pk(),
        _ts("ran_at"),
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("rows_deleted", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        # A negative count means the worker mis-read a rowcount. Worth failing
        # on rather than storing: the value of this table is that its numbers
        # can be trusted without re-deriving them from what they describe.
        sa.CheckConstraint("rows_deleted >= 0", name="rows_deleted_non_negative"),
        sa.CheckConstraint("duration_ms >= 0", name="duration_non_negative"),
    )
    op.execute(
        "CREATE INDEX idx_retention_actions_recent ON retention_actions (ran_at DESC)"
    )
    # §12.2's index serves "what happened last night". This one serves "what
    # has ever been deleted from this table", which is the question a reviewer
    # chasing one missing row actually asks, and which the first index answers
    # only by scanning every pass since the worker was switched on.
    op.execute(
        "CREATE INDEX idx_retention_actions_table "
        "ON retention_actions (table_name, ran_at DESC)"
    )
    # 0003's default privileges granted these on every future table.
    op.execute(f"REVOKE UPDATE, DELETE ON retention_actions FROM {APP_ROLE}")

    # --- retention_holds (§12.4) -------------------------------------------
    op.create_table(
        "retention_holds",
        _uuid_pk(),
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("row_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        # SET NULL rather than CASCADE: the hold outlives the operator who
        # placed it, and deleting the reviewer's account must not quietly
        # release evidence being kept for a dispute.
        sa.Column(
            "placed_by",
            PgUUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _ts("placed_at"),
        _ts("released_at", nullable=True),
        sa.Column("released_reason", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("length(reason) > 0", name="reason_not_empty"),
        sa.CheckConstraint(
            "released_at IS NULL OR released_at >= placed_at",
            name="released_after_placed",
        ),
    )
    # The worker's per-policy lookup. Unique and partial: one live hold per
    # row, and a released hold must not slow down the question "is this row
    # protected right now".
    op.execute(
        """
        CREATE UNIQUE INDEX idx_retention_holds_active
            ON retention_holds (table_name, row_id)
         WHERE released_at IS NULL
        """
    )
    # §7's leading index for the foreign key, and the "what is this operator
    # still holding" query an access review runs (§6.4).
    op.execute(
        "CREATE INDEX idx_retention_holds_placed_by ON retention_holds (placed_by)"
    )
    op.execute(
        """
        CREATE TRIGGER trg_retention_holds_updated_at
        BEFORE UPDATE ON retention_holds
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )

    # --- signing_keys.compromised_at (§11.4) -------------------------------
    op.add_column(
        "signing_keys",
        sa.Column("compromised_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "compromised_after_activated",
        "signing_keys",
        "compromised_at IS NULL OR compromised_at >= activated_at",
    )


def downgrade() -> None:
    # The bare name, not the prefixed one. env.py applies the metadata naming
    # convention on top of whatever is passed, so a name that already carries
    # its `ck_signing_keys_` prefix comes back out as
    # `ck_signing_keys_ck_signing_keys_compromised_after_activated` and the
    # DROP fails on a constraint that does not exist. Same hazard the
    # evaluation workflow's "Constraint names are not double-prefixed" step
    # guards against on the upgrade side; this is its downgrade twin, and it is
    # only findable by actually running the downgrade.
    op.drop_constraint(
        "compromised_after_activated", "signing_keys", type_="check"
    )
    op.drop_column("signing_keys", "compromised_at")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_retention_holds_updated_at ON retention_holds"
    )
    op.drop_table("retention_holds")

    # Restore what 0003's default privileges would have granted, so a
    # re-upgrade's REVOKE is not a no-op against a state it did not create.
    op.execute(f"GRANT UPDATE, DELETE ON retention_actions TO {APP_ROLE}")
    op.drop_table("retention_actions")
