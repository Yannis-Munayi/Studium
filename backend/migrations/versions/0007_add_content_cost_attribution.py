"""add content_artifacts.generated_for_session_id

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-15

Spec v1.1 §6.12 states the attribution rule for content-generation cost:
"attributed to the learner in whose session the generation was triggered.
Curator-initiated pre-generation (no requesting learner) is attributed to the
system synthetic user."

``content_artifacts`` has no session or user column, so there is no join path
from an artifact to the learner who triggered it -- the rule cannot be
implemented as written. This adds the missing link: nullable, because
pre-generation genuinely has no requesting session, and NULL is what routes the
cost to the system account in the roll-up.

``SET NULL`` rather than ``CASCADE``: an artifact outlives the session that
prompted it, and losing the artifact when a session ages out under the two-year
retention would be the wrong trade entirely.

Additive: one nullable column and a partial index, no downtime.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "content_artifacts",
        sa.Column("generated_for_session_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_content_artifacts_generated_for_session_id",
        "content_artifacts",
        "learning_sessions",
        ["generated_for_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_artifacts_generated_for_session",
        "content_artifacts",
        ["generated_for_session_id"],
        postgresql_where=sa.text("generated_for_session_id IS NOT NULL"),
    )

    # §7 requires an index on every FK. The two nullable FKs added in 0001 were
    # indexed unconditionally; narrow them to match the partial form §7 now
    # names as the preferred shape for nullable references.
    for column in ("reviewed_by", "superseded_by"):
        op.drop_index(f"idx_artifacts_{column}", table_name="content_artifacts")
        op.create_index(
            f"idx_artifacts_{column}",
            "content_artifacts",
            [column],
            postgresql_where=sa.text(f"{column} IS NOT NULL"),
        )


def downgrade() -> None:
    for column in ("reviewed_by", "superseded_by"):
        op.drop_index(f"idx_artifacts_{column}", table_name="content_artifacts")
        op.create_index(
            f"idx_artifacts_{column}", "content_artifacts", [column]
        )

    op.drop_index("idx_artifacts_generated_for_session", table_name="content_artifacts")
    op.drop_constraint(
        "fk_content_artifacts_generated_for_session_id",
        "content_artifacts",
        type_="foreignkey",
    )
    op.drop_column("content_artifacts", "generated_for_session_id")
