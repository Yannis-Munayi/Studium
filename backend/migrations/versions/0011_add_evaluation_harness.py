"""add the evaluation and assessment harness

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-25

The evaluation and assessment harness spec (subsystem 6) §5 names four additive
changes. This applies those four, plus three the spec assumes already exist or
never noticed it needed. All seven are additive: three new types (one enum,
two values on an existing enum), five new tables, one new column.

**golden_datasets / golden_dataset_entries** (§5 additions 1 and 2) hold the
authored cases, materialised from `content/evaluation/*.yaml` by
`studium eval sync`. The repo stays the source of truth (§4); these exist so a
scheduled run and a dashboard can query what the CI gate reads off disk.

**evaluation_runs / evaluation_results** (§5 additions 3 and 4) hold outcomes.
`prompt_hash` is a `CHAR(64)`-equivalent TEXT matching
`agent_traces.system_prompt_hash`, which is what makes §3's "prompt versions
are what get evaluated, not agents" enforceable rather than aspirational.

**signing_keys** is not in §5. §12 says portfolio credentials are "Backed by
data layer §6.10's `portfolio_items` and `signing_keys` tables"; §6.10 defines
only `portfolio_items`, and defers key management to subsystem 7 without
naming a table. `issuer_public_key_id` in §12.2's payload had nothing to point
at. See DIVERGENCES-EVALUATION (E1). Public key material only -- the private
half never touches the database.

**portfolio_item_kind gains 'assessment_pass' and 'subject_completion'**
(§12.1). The existing six values are learner *work* (proof, code, prose,
derivation, diagram, notebook); a credential is a different lifecycle and had
no value to be written under. Same divergence, E1.

**golden_datasets.regression_tolerance** (§13.2). The tolerance lives in the
dataset YAML and §5's DDL gives it no column, so a run driven from the database
rather than from a checkout had nowhere to read it. See E4.

Additive throughout. `ALTER TYPE ... ADD VALUE` is transactional from Postgres
12 on and nothing here uses the new values, so both are safe inside Alembic's
transaction. Existing rows are untouched.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

GOLDEN_DATASET_KINDS = (
    "agent_output",
    "retrieval_quality",
    "grading_calibration",
    "content_quality",
)

#: §12.1's two credential kinds, added to the end of portfolio_item_kind.
CREDENTIAL_KINDS = ("assessment_pass", "subject_completion")

#: The value order after both are added. The downgrade rebuilds the type to
#: drop them, which is the only way Postgres removes an enum value.
PORTFOLIO_KINDS_ORIGINAL = (
    "proof",
    "code",
    "prose",
    "derivation",
    "diagram",
    "notebook",
)
PORTFOLIO_KINDS_WITH_CREDENTIALS = PORTFOLIO_KINDS_ORIGINAL + CREDENTIAL_KINDS

GRADING_KINDS = ("deterministic", "meta_graded", "hybrid")
TRIGGER_KINDS = ("manual", "pre_deploy", "scheduled", "ci")
RUN_STATUSES = ("running", "complete", "failed")


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


def _updated_at_trigger(table: str) -> None:
    op.execute(
        f"""
        CREATE TRIGGER trg_{table}_updated_at
        BEFORE UPDATE ON {table}
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def upgrade() -> None:
    # --- portfolio_item_kind gains the two credential kinds (E1) -----------
    for value in CREDENTIAL_KINDS:
        op.execute(
            f"ALTER TYPE portfolio_item_kind ADD VALUE IF NOT EXISTS '{value}'"
        )

    # --- §5 addition 1: golden_datasets -----------------------------------
    values = ", ".join(f"'{v}'" for v in GOLDEN_DATASET_KINDS)
    op.execute(f"CREATE TYPE golden_dataset_kind AS ENUM ({values})")

    op.create_table(
        "golden_datasets",
        _uuid_pk(),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "kind",
            # postgresql.ENUM with create_type=False, not sa.Enum: sa.Enum
            # accepts the flag and ignores it, so create_table would re-emit
            # the CREATE TYPE three lines above and fail with DuplicateObject.
            postgresql.ENUM(
                *GOLDEN_DATASET_KINDS, name="golden_dataset_kind", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "agent",
            postgresql.ENUM(
                "learner",
                "orchestrator",
                "curator",
                "lecturer",
                "tutor",
                "evaluator",
                "confusion_tracker",
                "reviewer",
                "system",
                name="agent_identity",
                create_type=False,
            ),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "entry_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "regression_tolerance",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        _ts("created_at"),
        _ts("updated_at"),
        # An agent_output dataset with no agent is invisible to §13.4's
        # affected-dataset lookup, which resolves by agent identity -- so a
        # Lecturer prompt change would ship having evaluated nothing, silently.
        sa.CheckConstraint(
            "(kind IN ('agent_output', 'grading_calibration')) = (agent IS NOT NULL)",
            name="agent_matches_kind",
        ),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.CheckConstraint(
            "entry_count >= 0", name="entry_count_non_negative"
        ),
    )
    op.create_index("idx_golden_datasets_kind", "golden_datasets", ["kind", "active"])
    op.create_index(
        "idx_golden_datasets_agent",
        "golden_datasets",
        ["agent"],
        postgresql_where=sa.text("agent IS NOT NULL AND active"),
    )
    _updated_at_trigger("golden_datasets")

    # --- §5 addition 2: golden_dataset_entries ----------------------------
    op.create_table(
        "golden_dataset_entries",
        _uuid_pk(),
        sa.Column(
            "dataset_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("golden_datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entry_index", sa.Integer(), nullable=False),
        # Not in §5's DDL. §7.2's YAML gives every entry a stable `id:`
        # (entry_001); without a column for it a failing entry can be named
        # only by index, and indices shift when an entry is inserted -- which
        # would silently repoint every reviewer note written against a number.
        # See DIVERGENCES-EVALUATION (E3).
        sa.Column("entry_key", sa.Text(), nullable=False),
        sa.Column("input", JSONB(), nullable=False),
        sa.Column("expected", JSONB(), nullable=False),
        sa.Column("grading_kind", sa.Text(), nullable=False),
        sa.Column("rubric", JSONB()),
        sa.Column("notes", sa.Text()),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint(
            "dataset_id", "entry_index", name="uq_golden_dataset_entries_dataset_id_entry_index"
        ),
        sa.UniqueConstraint("dataset_id", "entry_key", name="uq_dataset_entry_key"),
        sa.CheckConstraint(
            f"grading_kind IN {GRADING_KINDS!r}",
            name="grading_kind_known",
        ),
        # §7.2: a meta-graded check "invokes the Evaluator ... with the rubric
        # provided in the YAML". With no rubric it would be asked to judge
        # against nothing and would return a confident number anyway.
        #
        # The rubric can live in either of two places, and the first draft of
        # this constraint only knew about one. §7.2's own worked example puts
        # it on the *property* -- `check: meta_graded` with `rubric: >` beneath
        # it -- so a hybrid entry with per-property rubrics has a NULL
        # entry-level `rubric` and is perfectly valid. `rubric IS NOT NULL`
        # alone rejected the shipped Lecturer dataset.
        #
        # jsonb_path_exists is immutable, so it is legal in a CHECK where the
        # subquery this would otherwise want is not. The predicate reads: no
        # meta_graded property is missing a non-empty rubric.
        sa.CheckConstraint(
            "grading_kind = 'deterministic'"
            " OR rubric IS NOT NULL"
            " OR NOT jsonb_path_exists("
            "      expected,"
            "      '$.properties[*] ? (@.\"check\" == \"meta_graded\""
            " && (!exists(@.rubric) || @.rubric == \"\"))')",
            name="meta_graded_needs_rubric",
        ),
        sa.CheckConstraint(
            "entry_index >= 0",
            name="entry_index_non_negative",
        ),
    )
    op.create_index(
        "idx_dataset_entries_dataset", "golden_dataset_entries", ["dataset_id"]
    )
    _updated_at_trigger("golden_dataset_entries")

    # --- §5 addition 3: evaluation_runs -----------------------------------
    op.create_table(
        "evaluation_runs",
        _uuid_pk(),
        sa.Column(
            "dataset_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("golden_datasets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("prompt_hash", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "triggered_by",
            PgUUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("trigger_kind", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'running'")
        ),
        sa.Column("aggregate_score", sa.REAL()),
        sa.Column(
            "entries_run", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "entries_passed", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "entries_failed", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        # NUMERIC, not the REAL §5's DDL asks for. Data layer §5: money is
        # NUMERIC. A run's cost is ~20 per-entry costs in the 10^-3 range
        # summed, and REAL's seven significant digits let the sum drift from
        # its parts. See DIVERGENCES-EVALUATION (E5).
        sa.Column(
            "cost_usd", sa.Numeric(12, 4), nullable=False, server_default=sa.text("0")
        ),
        _ts("started_at"),
        _ts("completed_at", nullable=True),
        sa.Column(
            "metadata", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        # TEXT plus a length CHECK rather than CHAR(64), per §5 of the data
        # layer spec: CHAR is blank-padded and compares by the padded value,
        # which is a real hazard for hash equality.
        sa.CheckConstraint(
            "length(prompt_hash) = 64", name="prompt_hash_len"
        ),
        sa.CheckConstraint(
            f"trigger_kind IN {TRIGGER_KINDS!r}",
            name="trigger_kind_known",
        ),
        sa.CheckConstraint(
            f"status IN {RUN_STATUSES!r}", name="status_known"
        ),
        sa.CheckConstraint(
            "aggregate_score IS NULL"
            " OR (aggregate_score >= 0.0 AND aggregate_score <= 1.0)",
            name="aggregate_score_range",
        ),
        # §5: "populated when complete". A complete run with no score is one
        # whose gate compares against NULL and silently passes.
        sa.CheckConstraint(
            "(status = 'complete') = (aggregate_score IS NOT NULL)",
            name="complete_has_score",
        ),
        sa.CheckConstraint(
            "entries_passed + entries_failed = entries_run",
            name="entry_counts_agree",
        ),
        sa.CheckConstraint(
            "cost_usd >= 0.0", name="cost_non_negative"
        ),
    )
    op.execute(
        """
        CREATE INDEX idx_eval_runs_dataset
            ON evaluation_runs (dataset_id, started_at DESC)
        """
    )
    op.create_index("idx_eval_runs_prompt", "evaluation_runs", ["prompt_hash"])
    # §13.1 step 4 compares against "the last passing run for the same
    # dataset" -- a filter on status ordered by completion within a dataset,
    # which neither of the spec's two indexes serves.
    op.execute(
        """
        CREATE INDEX idx_eval_runs_baseline
            ON evaluation_runs (dataset_id, completed_at DESC)
         WHERE status = 'complete'
        """
    )
    op.create_index(
        "idx_eval_runs_triggered_by",
        "evaluation_runs",
        ["triggered_by"],
        postgresql_where=sa.text("triggered_by IS NOT NULL"),
    )

    # --- §5 addition 4: evaluation_results --------------------------------
    op.create_table(
        "evaluation_results",
        _uuid_pk(),
        sa.Column(
            "run_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("evaluation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "entry_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("golden_dataset_entries.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("actual_output", JSONB(), nullable=False),
        sa.Column("score", sa.REAL(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("grading_notes", sa.Text()),
        sa.Column(
            "cost_usd", sa.Numeric(12, 4), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("latency_ms", sa.Integer()),
        # SET NULL rather than RESTRICT: agent_traces is subject to the §10
        # retention window, and a regression baseline that expires with its
        # traces in 90 days is not a baseline.
        sa.Column(
            "agent_trace_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("agent_traces.id", ondelete="SET NULL"),
        ),
        sa.Column("reviewer_note", sa.Text()),
        _ts("created_at"),
        sa.CheckConstraint(
            "score >= 0.0 AND score <= 1.0", name="score_range"
        ),
        sa.CheckConstraint(
            "cost_usd >= 0.0", name="cost_non_negative"
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="latency_non_negative",
        ),
        # Without this a retried entry writes a second row and the aggregate
        # double-counts it: 21 results for a 20-entry run, with entries_run
        # still saying 20 and the CHECK above none the wiser.
        sa.UniqueConstraint("run_id", "entry_id", name="uq_eval_results_run_entry"),
    )
    op.create_index("idx_eval_results_run", "evaluation_results", ["run_id"])
    op.create_index(
        "idx_eval_results_failures",
        "evaluation_results",
        ["run_id"],
        postgresql_where=sa.text("passed = FALSE"),
    )
    op.execute(
        """
        CREATE INDEX idx_eval_results_entry
            ON evaluation_results (entry_id, created_at DESC)
        """
    )
    # Not in §5's index list, and required by data layer §7 ("every FK has an
    # accompanying index"). agent_trace_id is SET NULL, so without it the §10
    # retention job deleting a day of expired traces sequential-scans every
    # evaluation result once per deleted trace.
    op.create_index(
        "idx_eval_results_trace",
        "evaluation_results",
        ["agent_trace_id"],
        postgresql_where=sa.text("agent_trace_id IS NOT NULL"),
    )

    # --- signing_keys (E1) -------------------------------------------------
    op.create_table(
        "signing_keys",
        _uuid_pk(),
        sa.Column("key_id", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "algorithm", sa.Text(), nullable=False, server_default=sa.text("'ed25519'")
        ),
        # Public half only. The private key lives wherever subsystem 7 puts
        # secrets: a signing key in a database is a signing key in every
        # backup, every replica, and every pg_dump anyone ever took.
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        _ts("activated_at"),
        _ts("retired_at", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "algorithm IN ('ed25519')", name="algorithm_known"
        ),
        sa.CheckConstraint(
            "length(public_key) BETWEEN 32 AND 128",
            name="public_key_len",
        ),
        sa.CheckConstraint(
            "retired_at IS NULL OR retired_at >= activated_at",
            name="retired_after_activated",
        ),
    )
    # At most one signing key per issuer at a time. Two current keys make
    # "which key signed this" a question the issuer cannot answer, and §12.3's
    # rotation is a swap rather than a fan-out.
    op.execute(
        """
        CREATE UNIQUE INDEX idx_signing_keys_current
            ON signing_keys (issuer)
         WHERE retired_at IS NULL
        """
    )
    _updated_at_trigger("signing_keys")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_signing_keys_updated_at ON signing_keys")
    op.drop_table("signing_keys")

    op.drop_table("evaluation_results")
    op.drop_table("evaluation_runs")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_golden_dataset_entries_updated_at "
        "ON golden_dataset_entries"
    )
    op.drop_table("golden_dataset_entries")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_golden_datasets_updated_at ON golden_datasets"
    )
    op.drop_table("golden_datasets")
    op.execute("DROP TYPE IF EXISTS golden_dataset_kind")

    # Postgres cannot drop an enum value, so the type is rebuilt without the
    # two credential kinds. Credential rows are deleted rather than relabelled
    # first: a credential re-labelled 'prose' would be a signed, externally
    # verifiable claim of mastery sitting in the learner's work portfolio,
    # which is worse than losing it -- and the signature would still verify.
    op.execute(
        f"DELETE FROM portfolio_items WHERE kind::text IN {CREDENTIAL_KINDS!r}"
    )
    op.execute("ALTER TYPE portfolio_item_kind RENAME TO portfolio_item_kind_old")
    values = ", ".join(f"'{v}'" for v in PORTFOLIO_KINDS_ORIGINAL)
    op.execute(f"CREATE TYPE portfolio_item_kind AS ENUM ({values})")
    op.execute(
        """
        ALTER TABLE portfolio_items
          ALTER COLUMN kind TYPE portfolio_item_kind
          USING kind::text::portfolio_item_kind
        """
    )
    op.execute("DROP TYPE portfolio_item_kind_old")
