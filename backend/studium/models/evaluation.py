"""Evaluation and assessment harness (evaluation spec §5).

Four tables the spec names -- ``golden_datasets``, ``golden_dataset_entries``,
``evaluation_runs``, ``evaluation_results`` -- plus ``signing_keys``, which §12
depends on and the data layer never defined (DIVERGENCES-EVALUATION E1).

The shape all four share is §1's: input, expected output, actual output,
verdict. What varies is who authors the input (always the reviewer, ahead of
time) and where the actual output comes from (an agent under test, or the
retriever). That is why one pair of run/result tables serves every dataset kind
rather than one pair per kind.

Two things here are *contracts* rather than storage decisions:

* **``prompt_hash`` matches ``agent_traces.system_prompt_hash``.** §3's last
  principle is that prompt versions get evaluated, not agents. Without the hash
  pinned on the run, comparing two runs compares nothing in particular. The
  index on it is what makes "has this exact prompt been evaluated before"
  answerable at gate time.
* **``evaluation_results.agent_trace_id``.** §5: a reviewer drills from a
  failed evaluation to the full trace. ``ON DELETE SET NULL`` rather than
  RESTRICT because ``agent_traces`` is subject to the §10 retention window and
  an evaluation result must outlive the trace it came from -- a regression
  baseline that expires in 90 days is not a baseline.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    REAL,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    Base,
    agent_identity,
    created_at,
    golden_dataset_kind,
    nullable_ts,
    sha256,
    sha256_check,
    updated_at,
    uuid_pk,
)

#: §13.1 step 3's ``trigger_kind`` values, and §6.2's list of when a run
#: happens. Constrained rather than left as free TEXT because §15.1 attributes
#: cost differently per trigger -- a typo'd value would book a scheduled
#: system run to a real user's ledger.
TRIGGER_KINDS = ("manual", "pre_deploy", "scheduled", "ci")

#: A run is running, then exactly one of complete or failed. §16's third row
#: ("dataset entry has invalid input") is the only path to ``failed``: a run
#: whose individual entries fail is *complete* with failures, which is what the
#: aggregate is for.
RUN_STATUSES = ("running", "complete", "failed")

#: §5's column comment says "'deterministic' or 'meta_graded'". §7.2's own
#: worked example declares ``grading_kind: hybrid``, and hybrid is the common
#: case -- the example entry mixes two deterministic properties with one
#: meta-graded one. Three values, not two. See DIVERGENCES-EVALUATION (E2).
GRADING_KINDS = ("deterministic", "meta_graded", "hybrid")


class GoldenDataset(Base):
    """§5 addition 1. One dataset answers one evaluation question (§7.1).

    ``active`` is the retirement mechanism (§7.3): entries are versioned but
    never deleted, so an obsolete dataset is deactivated and succeeded rather
    than edited. The old rows stay queryable, which is what makes a
    year-over-year comparison possible at all.
    """

    __tablename__ = "golden_datasets"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(golden_dataset_kind, nullable=False)
    #: Populated for ``agent_output`` and ``grading_calibration``; NULL for the
    #: two kinds that test something other than one agent's response.
    agent: Mapped[str | None] = mapped_column(agent_identity)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    #: Denormalised count of this dataset's entries.
    #:
    #: The spec gives it ``DEFAULT 0`` and never names a writer, so left alone
    #: it reads 0 forever while the dataset has 20 entries -- and §14.3's
    #: queue-depth dashboards read exactly this sort of column.
    #: ``studium.eval.sync`` recomputes it inside the sync transaction; the
    #: §17 Tier 2 test asserts it matches ``COUNT(*)`` afterwards. See
    #: DIVERGENCES-EVALUATION (E4).
    entry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: §13.2's per-dataset regression tolerance, materialised from the YAML.
    #:
    #: §13.2 puts this in the dataset file and §5's DDL gives it no column, so
    #: the gate had nowhere to read it from once the dataset was in the
    #: database. Keeping it in the YAML only would work for the CI gate (which
    #: has the repo checked out) and fail for the scheduled run (which does
    #: not). See DIVERGENCES-EVALUATION (E4).
    regression_tolerance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    entries: Mapped[list[GoldenDatasetEntry]] = relationship(
        back_populates="dataset", passive_deletes=True
    )

    __table_args__ = (
        # §7.1: agent_output tests "an agent's response", so it needs to know
        # which agent. The spec's DDL leaves the column merely nullable, which
        # would let a Lecturer dataset land with no agent and be silently
        # excluded from every "affected datasets" calculation in §13.4 -- the
        # exact failure mode where a prompt change ships unevaluated.
        CheckConstraint(
            "(kind IN ('agent_output', 'grading_calibration')) = (agent IS NOT NULL)",
            name="agent_matches_kind",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("entry_count >= 0", name="entry_count_non_negative"),
        Index("idx_golden_datasets_kind", "kind", "active"),
        # §13.4 resolves a prompt change to the datasets it affects by agent
        # identity. Without this the lookup sequential-scans, which is cheap
        # now and is also the query the CI gate runs on every push.
        Index(
            "idx_golden_datasets_agent",
            "agent",
            postgresql_where=text("agent IS NOT NULL AND active"),
        ),
    )


class GoldenDatasetEntry(Base):
    """§5 addition 2. One authored case: input, expected properties, rubric.

    Authored by a human, always (§3 "Golden datasets are authored, not
    generated"). Nothing in this package writes an entry from a model's output;
    ``studium review flag --to-dataset`` writes a *stub* whose ``expected`` is
    empty and which ``studium.eval.datasets`` refuses to run until a reviewer
    has filled it in.
    """

    __tablename__ = "golden_dataset_entries"

    id: Mapped[uuid.UUID] = uuid_pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("golden_datasets.id", ondelete="CASCADE"),
        nullable=False,
    )
    entry_index: Mapped[int] = mapped_column(Integer, nullable=False)
    #: §7.2's ``id:`` field (``entry_001``). The spec's DDL has no column for
    #: it, so a failing entry could be reported only by index -- and indices
    #: shift when an entry is inserted, which would silently repoint every
    #: reviewer note written against a number. Stable, human-authored, unique
    #: per dataset. See DIVERGENCES-EVALUATION (E3).
    entry_key: Mapped[str] = mapped_column(Text, nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    grading_kind: Mapped[str] = mapped_column(Text, nullable=False)
    rubric: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    dataset: Mapped[GoldenDataset] = relationship(back_populates="entries")

    __table_args__ = (
        UniqueConstraint("dataset_id", "entry_index"),
        UniqueConstraint("dataset_id", "entry_key", name="uq_dataset_entry_key"),
        CheckConstraint(f"grading_kind IN {GRADING_KINDS!r}", name="grading_kind_known"),
        # A meta-graded entry with no rubric cannot be graded: §7.2 says the
        # check "invokes the Evaluator ... with the rubric provided in the
        # YAML". Without one the meta-grader would be asked to judge against
        # nothing and would return a confident number anyway.
        #
        # The rubric lives in *either* of two places. §7.2's worked example
        # puts it on the property (`check: meta_graded` with `rubric:`
        # beneath), so a hybrid entry with per-property rubrics has a NULL
        # entry-level ``rubric`` and is valid. ``rubric IS NOT NULL`` alone
        # rejected the shipped Lecturer dataset -- caught by the Tier 2 sync
        # test, not by anything at this layer, because the ORM never inserts
        # the real content tree.
        #
        # ``jsonb_path_exists`` is immutable and therefore legal in a CHECK,
        # where the subquery this would otherwise want is not. Reads as: no
        # meta_graded property is missing a non-empty rubric.
        CheckConstraint(
            "grading_kind = 'deterministic'"
            " OR rubric IS NOT NULL"
            " OR NOT jsonb_path_exists("
            "      expected,"
            "      '$.properties[*] ? (@.\"check\" == \"meta_graded\""
            " && (!exists(@.rubric) || @.rubric == \"\"))')",
            name="meta_graded_needs_rubric",
        ),
        CheckConstraint("entry_index >= 0", name="entry_index_non_negative"),
        Index("idx_dataset_entries_dataset", "dataset_id"),
    )


class EvaluationRun(Base):
    """§5 addition 3. One execution of one dataset against one pinned prompt."""

    __tablename__ = "evaluation_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    #: RESTRICT, per the spec. Note that ``golden_dataset_entries.dataset_id``
    #: is CASCADE, so a dataset delete would cascade to entries and then be
    #: refused by ``evaluation_results.entry_id``'s RESTRICT -- the delete
    #: fails either way, just with a less obvious error. §7.3 says datasets are
    #: retired, never deleted, so this is belt and braces on a path nothing
    #: takes. See DIVERGENCES-EVALUATION (E6).
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("golden_datasets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: Matches ``agent_traces.system_prompt_hash`` (§3, §5).
    prompt_hash: Mapped[str] = sha256()
    model: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    trigger_kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'running'")
    )
    aggregate_score: Mapped[float | None] = mapped_column(REAL)
    entries_run: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    entries_passed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    entries_failed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: NUMERIC, not the ``REAL`` §5's DDL specifies. Data layer §5 is explicit
    #: that money is NUMERIC, and the §14 convention test enforces it on every
    #: ``*_usd`` column in the schema. A run's cost is the sum of ~20 per-entry
    #: costs each in the 10^-3 range; REAL carries about seven significant
    #: digits, so the sum drifts from its parts in the fourth decimal -- the
    #: exact failure ``cost_ledger.cost_usd`` was made generated-from-parts to
    #: prevent. See DIVERGENCES-EVALUATION (E5).
    cost_usd: Mapped[float] = mapped_column(
        Numeric(12, 4), nullable=False, server_default=text("0")
    )
    started_at: Mapped[dt.datetime] = created_at()
    completed_at: Mapped[dt.datetime | None] = nullable_ts()
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    results: Mapped[list[EvaluationResult]] = relationship(
        back_populates="run", passive_deletes=True
    )

    __table_args__ = (
        sha256_check("prompt_hash"),
        CheckConstraint(f"trigger_kind IN {TRIGGER_KINDS!r}", name="trigger_kind_known"),
        CheckConstraint(f"status IN {RUN_STATUSES!r}", name="status_known"),
        CheckConstraint(
            "aggregate_score IS NULL"
            " OR (aggregate_score >= 0.0 AND aggregate_score <= 1.0)",
            name="aggregate_score_range",
        ),
        # §5: "populated when complete". A complete run with no score is a run
        # whose gate would compare against NULL and silently pass.
        CheckConstraint(
            "(status = 'complete') = (aggregate_score IS NOT NULL)",
            name="complete_has_score",
        ),
        CheckConstraint(
            "entries_passed + entries_failed = entries_run", name="entry_counts_agree"
        ),
        CheckConstraint("cost_usd >= 0.0", name="cost_non_negative"),
        Index("idx_eval_runs_dataset", "dataset_id", text("started_at DESC")),
        Index("idx_eval_runs_prompt", "prompt_hash"),
        # §13.1 step 4 compares against "the last passing run for the same
        # dataset". That query filters on status and orders by time within a
        # dataset; the spec's two indexes serve neither shape.
        Index(
            "idx_eval_runs_baseline",
            "dataset_id",
            text("completed_at DESC"),
            postgresql_where=text("status = 'complete'"),
        ),
        Index(
            "idx_eval_runs_triggered_by",
            "triggered_by",
            postgresql_where=text("triggered_by IS NOT NULL"),
        ),
    )


class EvaluationResult(Base):
    """§5 addition 4. One entry's outcome within one run."""

    __tablename__ = "evaluation_results"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("evaluation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    entry_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("golden_dataset_entries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actual_output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    score: Mapped[float] = mapped_column(REAL, nullable=False)
    passed: Mapped[bool] = mapped_column(nullable=False)
    grading_notes: Mapped[str | None] = mapped_column(Text)
    #: NUMERIC for the reason on ``EvaluationRun.cost_usd`` (E5).
    cost_usd: Mapped[float] = mapped_column(
        Numeric(12, 4), nullable=False, server_default=text("0")
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    #: SET NULL, not RESTRICT: see the module docstring. A result must outlive
    #: the trace behind it or every baseline expires with the retention window.
    agent_trace_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("agent_traces.id", ondelete="SET NULL")
    )
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = created_at()

    run: Mapped[EvaluationRun] = relationship(back_populates="results")

    __table_args__ = (
        CheckConstraint("score >= 0.0 AND score <= 1.0", name="score_range"),
        CheckConstraint("cost_usd >= 0.0", name="cost_non_negative"),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0", name="latency_non_negative"
        ),
        # One result per entry per run. Without it a retried entry writes a
        # second row and the aggregate double-counts it -- a run of 20 entries
        # reporting 21 results, with entries_run saying 20.
        UniqueConstraint("run_id", "entry_id", name="uq_eval_results_run_entry"),
        Index("idx_eval_results_run", "run_id"),
        Index(
            "idx_eval_results_failures",
            "run_id",
            postgresql_where=text("passed = FALSE"),
        ),
        Index("idx_eval_results_entry", "entry_id", text("created_at DESC")),
        # Not in §5's index list. Postgres does not index the referencing side
        # of a foreign key, and this one is SET NULL: the §10 retention job
        # deleting a day of expired traces would sequential-scan every
        # evaluation result once per deleted trace.
        Index(
            "idx_eval_results_trace",
            "agent_trace_id",
            postgresql_where=text("agent_trace_id IS NOT NULL"),
        ),
    )


class SigningKey(Base):
    """Ed25519 issuer keys for portfolio credentials (§12.3).

    DIVERGENCES-EVALUATION (E1): §12 says this is "data layer §6.10's ...
    ``signing_keys`` table". There is no such table in the data layer -- §6.10
    defines ``portfolio_items`` only, and its ``signature`` column is described
    as "signed with a server-held key" with key management deferred to the
    infrastructure spec. So the table §12 builds on had to be built.

    **No private key material is stored here.** Only the public key, its
    identifier, and the validity window. The private half lives wherever
    subsystem 7 puts secrets; ``studium.eval.credentials`` loads it from the
    environment and never writes it anywhere. A signing key in a database is a
    signing key in every backup, every read replica, and every ``pg_dump`` in a
    developer's downloads folder.

    Retired keys are kept, never deleted (§12.3 "Old public keys remain
    published so historical portfolio items stay verifiable"). Deleting one
    would invalidate every credential it ever signed.
    """

    __tablename__ = "signing_keys"

    id: Mapped[uuid.UUID] = uuid_pk()
    #: Stable public identifier, quoted in the credential's
    #: ``issuer_public_key_id`` (§12.2). Human-legible so a verifier can tell
    #: two keys apart in a published key list.
    key_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    algorithm: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'ed25519'")
    )
    #: Base64, 32 raw bytes for Ed25519 -> 44 characters with padding.
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: 'studium.app' (§12.2). A column rather than a constant because a
    #: self-hosted deployment issuing under its own name must not be able to
    #: produce credentials that claim to be ours.
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    activated_at: Mapped[dt.datetime] = created_at()
    #: Set when the key rotates (§12.3, annually). The key stays published and
    #: stays valid for what it already signed; it just stops signing.
    retired_at: Mapped[dt.datetime | None] = nullable_ts()
    #: Infrastructure §11.4, migration 0012. The *earliest time the key may have
    #: been compromised*, not the time the operator noticed -- §11.4 asks that
    #: verifiers be told which items "signed with the compromised key between
    #: the earliest possible compromise and rotation should be treated with
    #: additional scrutiny", and only the earlier of the two bounds that window
    #: correctly.
    #:
    #: Deliberately advisory. A compromised key still *verifies* what it
    #: signed -- the mathematics is unchanged, and turning every credential it
    #: ever issued invalid would punish the learners rather than the attacker.
    #: ``credentials.verify_item`` reports it alongside ``valid`` so the
    #: verifier decides. See DIVERGENCES-INFRASTRUCTURE (N4).
    compromised_at: Mapped[dt.datetime | None] = nullable_ts()
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    __table_args__ = (
        CheckConstraint("algorithm IN ('ed25519',)", name="algorithm_known"),
        CheckConstraint("length(public_key) BETWEEN 32 AND 128", name="public_key_len"),
        CheckConstraint(
            "retired_at IS NULL OR retired_at >= activated_at",
            name="retired_after_activated",
        ),
        # A key cannot have been compromised before it existed. Without this a
        # fat-fingered date silently widens the scrutiny window §11.4 step 3
        # publishes to downstream verifiers.
        CheckConstraint(
            "compromised_at IS NULL OR compromised_at >= activated_at",
            name="compromised_after_activated",
        ),
        # At most one key signs at a time. Two current keys would make "which
        # key signed this" a question the issuer itself could not answer, and
        # §12.3's rotation is a swap, not a fan-out.
        Index(
            "idx_signing_keys_current",
            "issuer",
            unique=True,
            postgresql_where=text("retired_at IS NULL"),
        ),
    )
