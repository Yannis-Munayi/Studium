"""retain credentials through learner erasure

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-04

Amendment v1.2.1 §3.1: a credential has to survive the erasure of the learner
who earned it, because the whole point of one is that a third party can verify
it long after issuance. The schema did the opposite. ``portfolio_items`` hangs
off ``users`` with ``ON DELETE CASCADE`` and off ``learner_subjects`` with
another, and §10's erasure deletes the enrollment outright -- so the first
learner to earn a credential and then exercise right-to-erasure would lose it,
and every external verifier holding a copy would find §12.4's endpoint no
longer resolves the id.

Three changes, each load-bearing for that one outcome:

**``is_credential``, with a CHECK that keeps it honest.** The discriminator
already exists -- ``kind IN ('assessment_pass', 'subject_completion')``, which
is what ``credentials.verify_item`` filters on -- so a boolean beside it is
redundant by construction. It is added anyway, and the CHECK is why: erasure
has to select credentials in the same transaction that deletes everything else
the learner owns, and a predicate that reads ``AND is_credential`` cannot be
misread at 02:00 the way an enum membership test can. The CHECK means the
column cannot drift from the enum, so the redundancy costs nothing and the
denormalisation can never lie. Adding a third credential kind is then a
migration that has to update both, which is the correct amount of friction.

**``learner_subject_id`` becomes nullable.** This is the concession, and it is
unavoidable rather than chosen. The enrollment is learner-owned data that §10
purges; a retained credential cannot keep pointing at it. Nulling that column
is also what satisfies the composite enrollment foreign key, which is MATCH
SIMPLE: a row with any NULL in the key is not required to have a referent. So
the credential detaches from the deleted enrollment without the FK needing to
change at all.

``uq_portfolio_chain (learner_subject_id, chain_index)`` tolerates this: NULLs
are distinct by default, so every erased learner's retained credentials keep
whatever ``chain_index`` they had without colliding.

**The reserved anonymised-learner account.** ``user_id`` stays NOT NULL --
amendment §3.1 offers making it nullable as the alternative and the amendment
itself recommends against, because a nullable owner weakens the column for
every row in the table to serve the few that outlive their owner. Instead the
detached credential is reassigned to a fixed reserved id, parallel to migration
0008's system user and for the same reason: a recognisable row says "this had
an owner and no longer does", where NULL would have to be told apart from
"never had one".

**What erasure does not preserve, and should not.** The hash chain around the
credential goes with the learner: the neighbouring items are their work, and
retaining them to keep a chain intact would retain exactly what §10 exists to
remove. The credential's ``signature.manifest.prev_sha256`` therefore points at
a row that is gone. Verification is unaffected -- §12.3 signs the credential
payload in its own right (``signature.credential_signature``) and §12.4's
endpoint checks that signature against ``signing_keys``, never the chain. The
chain is tamper-evidence for a portfolio; the signature is the credential.

Additive apart from the one nullability relaxation. Existing rows are
backfilled from ``kind``, which is where the answer already was.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

#: Owner of record for a credential whose learner has been erased. Fixed and
#: reserved, in the same block as 0008's system user (...0001).
ANONYMIZED_LEARNER_ID = "00000000-0000-7000-8000-000000000002"
ANONYMIZED_LEARNER_EMAIL = "anonymized-learner@studium.internal"

#: evaluation §12.1's two credential kinds, as they appear in
#: ``portfolio_item_kind`` since migration 0011.
CREDENTIAL_KINDS = ("assessment_pass", "subject_completion")

#: ``kind::text``, not a bare enum literal, and this is not cosmetic.
#: ``ALTER TYPE ... ADD VALUE`` cannot be followed by a *use* of the new value
#: in the same transaction -- Postgres raises ``unsafe use of new value
#: "assessment_pass" of enum type portfolio_item_kind``. Migration 0011 adds
#: both values, and env.py wraps the whole ``upgrade head`` in one transaction,
#: so a deployment migrating a fresh database from base runs 0011 and 0013
#: inside it and hits exactly that. Comparing the text rendering references no
#: enum value at all, and is the same idiom ``credentials.verify_item``
#: already filters with. Found by ``test_full_migration_sequence``, which is
#: the only thing in the project that migrates from base in one transaction --
#: applying 0013 to an already-migrated database succeeds either way.
_KIND_PREDICATE = (
    "kind::text IN (" + ", ".join(f"'{k}'" for k in CREDENTIAL_KINDS) + ")"
)


def upgrade() -> None:
    # --- the flag (§3.1) ---------------------------------------------------
    op.add_column(
        "portfolio_items",
        sa.Column(
            "is_credential",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )
    # Backfill from the enum rather than from anything the application says:
    # the credentials already in the table were written by
    # ``credentials.issue_credential``, and their kind is the record of what
    # they are.
    op.execute(
        f"UPDATE portfolio_items SET is_credential = TRUE WHERE {_KIND_PREDICATE}"
    )
    op.create_check_constraint(
        "is_credential_matches_kind",
        "portfolio_items",
        f"is_credential = ({_KIND_PREDICATE})",
    )
    # Serves both readers: erasure's "which of this learner's items survive"
    # and the monthly credential audit's "what is currently retained". Partial,
    # because credentials are a small minority of portfolio rows and an index
    # over the essays would be paid for on every insert.
    op.execute(
        """
        CREATE INDEX idx_portfolio_credentials
            ON portfolio_items (user_id, created_at DESC)
         WHERE is_credential
        """
    )

    # --- detachability -----------------------------------------------------
    #
    # The composite FK is left exactly as it is. MATCH SIMPLE means a row with
    # a NULL learner_subject_id satisfies it whatever user_id holds, so making
    # the column nullable is the entire mechanism; ON DELETE CASCADE still
    # governs every row that has not been detached first, which is every
    # non-credential item.
    op.alter_column(
        "portfolio_items",
        "learner_subject_id",
        existing_type=PgUUID(as_uuid=True),
        nullable=True,
    )

    # --- the reserved account ----------------------------------------------
    #
    # No password_hash and no auth_sessions: like 0008's system user, this
    # account cannot be logged into. No user_budget_caps either, and that is
    # the difference between the two -- the system user owns cost_ledger rows
    # and so needs a caps row for `budget_status` to read; this one owns
    # nothing but detached credentials and never spends. A caps row would
    # assert a spending relationship that does not exist.
    op.execute(
        f"""
        INSERT INTO users (id, email, display_name, role, auth_provider, password_hash)
        VALUES (
            '{ANONYMIZED_LEARNER_ID}',
            '{ANONYMIZED_LEARNER_EMAIL}',
            'Anonymized former learner',
            'learner',
            'local',
            NULL
        )
        ON CONFLICT (id) DO NOTHING
        """
    )


def downgrade() -> None:
    # A detached credential has no enrollment to be re-attached to -- the
    # learner_subjects row it named was deleted by the erasure that detached
    # it, and nothing in the database remembers which one it was. Restoring
    # NOT NULL therefore means deleting those rows, which is precisely the data
    # loss this migration exists to prevent, so the downgrade refuses instead
    # of choosing for the operator. Data layer §12.4's downgrade sign-off is
    # the process that resolves it: either accept the loss and delete them by
    # hand, or stay on 0013.
    detached = op.get_bind().exec_driver_sql(
        "SELECT count(*) FROM portfolio_items WHERE learner_subject_id IS NULL"
    ).scalar()
    if detached:
        raise RuntimeError(
            f"{detached} portfolio item(s) have been detached from their "
            f"enrollment by a right-to-erasure request (amendment v1.2.1 "
            f"§3.1). Downgrading past 0013 restores learner_subject_id NOT "
            f"NULL, which cannot hold them, and there is no enrollment left to "
            f"re-attach them to. Delete them deliberately and re-run the "
            f"downgrade, or stay on 0013:\n"
            f"    DELETE FROM portfolio_items WHERE learner_subject_id IS NULL;"
        )

    op.execute(f"DELETE FROM users WHERE id = '{ANONYMIZED_LEARNER_ID}'")

    op.alter_column(
        "portfolio_items",
        "learner_subject_id",
        existing_type=PgUUID(as_uuid=True),
        nullable=False,
    )
    op.execute("DROP INDEX IF EXISTS idx_portfolio_credentials")
    # The bare name, not the prefixed one -- env.py applies the metadata naming
    # convention on top of whatever is passed, and a name that already carries
    # its `ck_portfolio_items_` prefix comes back out doubled. Same hazard 0012
    # documents on its own downgrade.
    op.drop_constraint(
        "is_credential_matches_kind", "portfolio_items", type_="check"
    )
    op.drop_column("portfolio_items", "is_credential")
