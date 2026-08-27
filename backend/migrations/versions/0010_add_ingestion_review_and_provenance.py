"""add the ingestion review queue and the extraction provenance columns

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-25

The content authoring and ingestion spec (subsystem 5) §5 names four additive
changes and §7.3 a fifth. This applies all five, plus one the spec did not
notice it needed.

**ingestion_review_queue** (§5 addition 1) is the S3 resolution.
``content_review_queue``'s ``has_target`` CHECK requires an artifact or a
session turn; an ingestion failure has neither, so retrieval's embedding worker
could not enqueue a chunk that failed to embed and logged it instead. A chunk
with no vector is invisible to vector search, so the failure was real and the
only record of it was a log line. This table takes source, chunk, subject or
concept as the target, which is what ingestion-side failures actually point at.

A separate table rather than a relaxed CHECK: dropping ``has_target`` would
also let a *content* review row through with no target, and that constraint is
correct for the rows it governs.

**sources.extractor_version / normalizer_version** (§5 addition 2) record which
code produced the text currently on a source. Nullable: the draft carry-forward
corpus was ingested by a pipeline that recorded neither, and back-filling a
guess would be exactly the confident-wrong-provenance §13 exists to forbid.

**source_chunks.extraction_confidence** (§5 addition 3) with a partial index
under 0.7 for the reviewer's "what is doubtful in this source" query.

**source_chunks.superseded_at** (§7.3) marks chunks replaced by a
re-extraction. They stay: ``content_citations.source_chunk_id`` is
``ON DELETE RESTRICT``, so a citation written against an old chunk must keep
resolving. Retrieval filters them out of new results instead, via the partial
index on the live rows.

**ingestion_job_kind gains 'normalize'.** Not in the spec's list, and needed by
it: §6.4 triggers the normalize stage on ``ingestion_jobs`` rows with
``kind = 'normalize'``, a value the enum did not have. See
DIVERGENCES-INGESTION (I1).

Additive throughout -- one new type, one new value on an existing type, one new
table, four new columns, five new indexes. Existing rows are untouched beyond
the two column defaults, which Postgres 16 applies without a rewrite.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

INGESTION_FLAG_SOURCES = (
    "extractor_failure",
    "normalizer_warning",
    "embedding_failure",
    "chunk_ambiguous_type",
    "license_pending",
    "license_conflict",
    "concept_source_conflict",
    "graph_validation_error",
    "rubric_validation_error",
)

#: The value order after 'normalize' is added. Used by the downgrade, which has
#: to rebuild the type to drop a value.
JOB_KINDS_WITH_NORMALIZE = (
    "extract_text",
    "normalize",
    "chunk",
    "embed",
    "suggest_concept_mapping",
)
JOB_KINDS_ORIGINAL = tuple(k for k in JOB_KINDS_WITH_NORMALIZE if k != "normalize")


def upgrade() -> None:
    # --- ingestion_job_kind gains 'normalize' ------------------------------
    #
    # ADD VALUE is transactional from Postgres 12 on, but the new value cannot
    # be *used* until this transaction commits. Nothing below uses it, so this
    # is safe inside Alembic's transaction.
    op.execute(
        "ALTER TYPE ingestion_job_kind ADD VALUE IF NOT EXISTS 'normalize' "
        "AFTER 'extract_text'"
    )

    # --- §5 addition 1: the ingestion review queue -------------------------
    values = ", ".join(f"'{v}'" for v in INGESTION_FLAG_SOURCES)
    op.execute(f"CREATE TYPE ingestion_flag_source AS ENUM ({values})")

    op.create_table(
        "ingestion_review_queue",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "source_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "source_chunk_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_chunks.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "subject_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subjects.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "concept_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("concepts.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "flag_source",
            # postgresql.ENUM, not sa.Enum. `create_type=False` is a
            # PostgreSQL-dialect flag: sa.Enum accepts it silently and ignores
            # it, so create_table would re-emit CREATE TYPE for the type
            # created three lines above and fail with DuplicateObject. The two
            # spellings look interchangeable and are not.
            postgresql.ENUM(
                *INGESTION_FLAG_SOURCES,
                name="ingestion_flag_source",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "severity", sa.SmallInteger(), nullable=False, server_default=sa.text("2")
        ),
        sa.Column(
            "status",
            # Reuses the review_status type created in 0001 -- same reason as
            # above: sa.Enum here would try to create it a second time.
            postgresql.ENUM(
                "pending",
                "in_review",
                "resolved",
                "dismissed",
                name="review_status",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "assigned_to",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("resolution_note", sa.Text()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column(
            "payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "source_id IS NOT NULL OR source_chunk_id IS NOT NULL "
            "OR subject_id IS NOT NULL OR concept_id IS NOT NULL",
            name="ck_ingestion_review_queue_has_target",
        ),
        sa.CheckConstraint(
            "severity BETWEEN 1 AND 3",
            name="ck_ingestion_review_queue_severity_range",
        ),
    )

    op.execute(
        """
        CREATE INDEX idx_ingestion_queue_pending
            ON ingestion_review_queue (severity DESC, created_at)
         WHERE status = 'pending'
        """
    )
    op.create_index(
        "idx_ingestion_queue_assigned",
        "ingestion_review_queue",
        ["assigned_to", "status"],
        postgresql_where=sa.text("assigned_to IS NOT NULL"),
    )
    # One per target column: all four cascade, and an unindexed referencing
    # side turns each cascade delete into a sequential scan of the queue.
    for column, index in (
        ("source_id", "idx_ingestion_queue_source"),
        ("source_chunk_id", "idx_ingestion_queue_chunk"),
        ("subject_id", "idx_ingestion_queue_subject"),
        ("concept_id", "idx_ingestion_queue_concept"),
    ):
        op.create_index(
            index,
            "ingestion_review_queue",
            [column],
            postgresql_where=sa.text(f"{column} IS NOT NULL"),
        )
    op.execute(
        """
        CREATE TRIGGER trg_ingestion_review_queue_updated_at
        BEFORE UPDATE ON ingestion_review_queue
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )

    # --- §5 addition 2: extractor and normalizer provenance ----------------
    op.add_column("sources", sa.Column("extractor_version", sa.Text()))
    op.add_column("sources", sa.Column("normalizer_version", sa.Text()))

    # --- §5 addition 3: per-chunk extraction confidence --------------------
    op.add_column(
        "source_chunks",
        sa.Column(
            "extraction_confidence",
            sa.REAL(),
            nullable=False,
            server_default=sa.text("1.0"),
        ),
    )
    op.create_check_constraint(
        "extraction_confidence_range",
        "source_chunks",
        "extraction_confidence >= 0.0 AND extraction_confidence <= 1.0",
    )
    op.create_index(
        "idx_source_chunks_low_confidence",
        "source_chunks",
        ["source_id", "extraction_confidence"],
        postgresql_where=sa.text("extraction_confidence < 0.7"),
    )

    # --- §7.3: re-extraction supersession ----------------------------------
    op.add_column(
        "source_chunks", sa.Column("superseded_at", sa.DateTime(timezone=True))
    )
    op.create_index(
        "idx_source_chunks_live",
        "source_chunks",
        ["source_id", "chunk_index"],
        postgresql_where=sa.text("superseded_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_source_chunks_live", table_name="source_chunks")
    op.drop_column("source_chunks", "superseded_at")

    op.drop_index("idx_source_chunks_low_confidence", table_name="source_chunks")
    # The bare name, not the rendered one. Alembic applies the metadata naming
    # convention (`ck_%(table_name)s_%(constraint_name)s`) on the way *out* as
    # well as in, so passing the already-prefixed name produces
    # `ck_source_chunks_ck_source_chunks_extraction_confidence_range` and the
    # drop fails on a constraint that does not exist under that spelling.
    op.drop_constraint(
        "extraction_confidence_range", "source_chunks", type_="check"
    )
    op.drop_column("source_chunks", "extraction_confidence")

    op.drop_column("sources", "normalizer_version")
    op.drop_column("sources", "extractor_version")

    op.execute("DROP TRIGGER trg_ingestion_review_queue_updated_at ON ingestion_review_queue")
    op.drop_table("ingestion_review_queue")
    op.execute("DROP TYPE ingestion_flag_source")

    # Postgres has no DROP VALUE. Rebuilding the type is the only way back, and
    # it is only safe because no row can hold 'normalize' at this point: the
    # jobs that use it are written by the ingestion pipeline this migration
    # introduces. Any that exist are deleted first rather than silently
    # rewritten to another kind -- a normalize job re-labelled 'chunk' would be
    # picked up by the chunk worker and run against text that was never
    # normalised.
    op.execute("DELETE FROM ingestion_jobs WHERE kind = 'normalize'")
    original = ", ".join(f"'{k}'" for k in JOB_KINDS_ORIGINAL)
    op.execute(f"CREATE TYPE ingestion_job_kind_old AS ENUM ({original})")
    op.execute(
        "ALTER TABLE ingestion_jobs "
        "ALTER COLUMN kind TYPE ingestion_job_kind_old "
        "USING kind::text::ingestion_job_kind_old"
    )
    op.execute("DROP TYPE ingestion_job_kind")
    op.execute("ALTER TYPE ingestion_job_kind_old RENAME TO ingestion_job_kind")
