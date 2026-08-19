"""add source_chunks.chunk_type and source_chunks.tsvector_text

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-19

Retrieval spec (subsystem 3) §5 names two additive columns on ``source_chunks``
and asks that they be folded into the data layer's v1.2 batch. They are here
rather than in a later revision because retrieval cannot be built without
either: hybrid search (§9) reads both on every query.

**chunk_type.** What kind of text a chunk is. Retrieval treats the kinds
differently -- headings and bibliography entries are excluded from results
because they are structurally present but useless as evidence, code and math
blocks stay atomic through chunking, exercise chunks are preferred for lab
problems. ``DEFAULT 'body'`` so existing rows migrate without a backfill; the
chunking algorithm assigns the real type at ingestion time from here on.

**tsvector_text.** A ``GENERATED ALWAYS AS ... STORED`` column carrying the
tsvector of the chunk text, indexed with GIN. It is the keyword half of §9's
hybrid search. Generated rather than trigger-maintained so it cannot drift from
``text``; stored rather than virtual because it is read on every keyword query
and Postgres cannot index a virtual generated column at all.

``to_tsvector('english', text)`` -- the two-argument form with an explicit
configuration -- is immutable, which is what makes it legal in a generated
column. The one-argument form reads ``default_text_search_config`` and is only
stable, so it would be rejected here. That is also why the language is fixed
for MVP: it is baked into the expression, not chosen per row.

Additive: two columns and two indexes, no rewrite of existing data beyond the
generated column's backfill, which is one pass over a table holding thousands
of rows at MVP scale.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

CHUNK_KINDS = (
    "body",
    "heading",
    "code",
    "math",
    "figure_caption",
    "exercise",
    "reference",
)


def upgrade() -> None:
    # Explicit CREATE TYPE rather than letting the column emit it: migration
    # 0001 established that enum creation stays under our control so creation
    # and drop order are ours to sequence (§13 notes autogenerate misses enum
    # evolution entirely).
    values = ", ".join(f"'{v}'" for v in CHUNK_KINDS)
    op.execute(f"CREATE TYPE chunk_kind AS ENUM ({values})")

    op.add_column(
        "source_chunks",
        sa.Column(
            "chunk_type",
            sa.Enum(*CHUNK_KINDS, name="chunk_kind", create_type=False),
            nullable=False,
            server_default=sa.text("'body'"),
        ),
    )
    op.create_index(
        "idx_source_chunks_type",
        "source_chunks",
        ["source_id", "chunk_type"],
    )

    op.add_column(
        "source_chunks",
        sa.Column(
            "tsvector_text",
            TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_source_chunks_tsvector",
        "source_chunks",
        ["tsvector_text"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("idx_source_chunks_tsvector", table_name="source_chunks")
    op.drop_column("source_chunks", "tsvector_text")

    op.drop_index("idx_source_chunks_type", table_name="source_chunks")
    op.drop_column("source_chunks", "chunk_type")

    op.execute("DROP TYPE chunk_kind")
