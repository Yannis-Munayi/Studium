"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Spec section 13 rules for this file:

* Every up-migration needs a working downgrade -- CI asserts it.
* Additive changes (new tables, nullable columns, indexes) deploy with no
  downtime. Non-additive ones (drop, rename, type change) need the two-step
  dance, or an explicit maintenance window at MVP scale.
* Schema changes and data backfills belong in separate revisions, so a failed
  backfill can be re-run without re-applying the schema change.
* Add a CHANGELOG.md entry describing the semantic change.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
