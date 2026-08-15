"""add concepts.module_slug

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-14

Spec section 12 maps the draft's ``Module`` onto "a grouping of concepts via a
nullable concepts.module_slug column added in migration 0002". The column is
named there but never appears in the section 6 DDL, so it lands here rather
than in the baseline -- which is also the honest place for it: modules are a
migration convenience for carrying the draft forward, not part of the graph
model. See DIVERGENCES.md (B4).

Additive: new nullable column plus an index, so this runs against a live
database with no downtime.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("concepts", sa.Column("module_slug", sa.Text(), nullable=True))
    op.create_index(
        "idx_concepts_module",
        "concepts",
        ["subject_id", "module_slug"],
        postgresql_where=sa.text("module_slug IS NOT NULL"),
    )
    op.create_check_constraint(
        "module_slug_format",
        "concepts",
        r"module_slug IS NULL OR module_slug ~ '^[a-z0-9][a-z0-9-]{1,63}$'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_concepts_module_slug_format", "concepts", type_="check")
    op.drop_index("idx_concepts_module", table_name="concepts")
    op.drop_column("concepts", "module_slug")
