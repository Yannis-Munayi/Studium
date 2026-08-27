"""Declarative base, column helpers, and the shared ENUM types from spec §6.0.

Conventions enforced here (spec §5):

* Primary keys are UUIDv7, column ``id``, ``DEFAULT uuid_generate_v7()``.
* ``created_at`` / ``updated_at`` are ``TIMESTAMPTZ NOT NULL DEFAULT NOW()`` on
  every mutable table; append-only event tables carry only ``created_at``.
* Strings are ``TEXT``. Fixed-width hashes are ``TEXT`` plus a length CHECK
  rather than ``CHAR(n)`` -- see DIVERGENCES.md (B9).

``updated_at`` is maintained by the ``set_updated_at()`` trigger installed in
migration 0001, not by the ORM, so a raw SQL write keeps the same guarantee.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, MetaData, Text, func, text
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Constraint naming so Alembic autogenerate produces stable, reviewable names.
# Index names are always given explicitly instead -- §7 and the query-shape
# tests in §14 refer to them by name.
NAMING_CONVENTION = {
    "ix": "idx_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }


TIMESTAMPTZ = DateTime(timezone=True)

# --- column helpers -------------------------------------------------------


def uuid_pk() -> Mapped[uuid.UUID]:
    """UUIDv7 primary key (§5). Time-ordered, so index locality is preserved."""
    return mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v7()"),
    )


def created_at() -> Mapped[dt.datetime]:
    return mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())


def updated_at() -> Mapped[dt.datetime]:
    """Maintained by the set_updated_at() trigger, not by the ORM."""
    return mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())


def nullable_ts() -> Mapped[dt.datetime | None]:
    return mapped_column(TIMESTAMPTZ, nullable=True)


def sha256(*, nullable: bool = False) -> Mapped[str]:
    """A SHA-256 hex digest.

    Spec §6 writes these as ``CHAR(64)``; §5 says "TEXT for everything, no
    VARCHAR(n)". CHAR is blank-padded and compares by the padded value, which
    is a real hazard for hash equality, so TEXT plus a length CHECK it is.
    """
    return mapped_column(Text, nullable=nullable)


def sha256_check(column: str) -> CheckConstraint:
    return CheckConstraint(f"length({column}) = 64", name=f"{column}_len")


# --- shared enum types (§6.0) ---------------------------------------------
#
# create_type=False: migration 0001 issues the CREATE TYPE statements
# explicitly so that enum creation and drop order stay under our control
# (§13 notes autogenerate misses enum evolution).


def _enum(name: str, *values: str) -> PgEnum:
    return PgEnum(*values, name=name, create_type=False, metadata=Base.metadata)


agent_identity = _enum(
    "agent_identity",
    "learner",
    "orchestrator",
    "curator",
    "lecturer",
    "tutor",
    "evaluator",
    "confusion_tracker",
    "reviewer",
    "system",
)

session_mode = _enum(
    "session_mode",
    "orientation",
    "lecture",
    "tutorial",
    "lab",
    "review",
    "office_hours",
    "summative_assessment",
)

artifact_status = _enum("artifact_status", "draft", "reviewed", "active", "retired")

license_kind = _enum(
    "license_kind",
    "public_domain",
    "cc_by",
    "cc_by_sa",
    "cc_by_nc",
    "user_uploaded",
    "permission_granted",
    "fair_use",
)

concept_edge_kind = _enum(
    "concept_edge_kind",
    "prerequisite",
    "dependency",
    "generalization",
    "application",
    "related",
)

concept_source_role = _enum(
    "concept_source_role",
    "canonical_definition",
    "primary_exposition",
    "worked_example",
    "exercise",
    "historical",
    "alternative_stance",
)

#: Retrieval spec §5 addition 1. What kind of text a chunk is, which decides
#: how retrieval treats it: headings never return as standalone evidence, code
#: and math blocks stay atomic through chunking, exercises are preferred for
#: lab problems.
chunk_kind = _enum(
    "chunk_kind",
    "body",
    "heading",
    "code",
    "math",
    "figure_caption",
    "exercise",
    "reference",
)

artifact_kind = _enum(
    "artifact_kind",
    "lecture_segment",
    "tutorial_seed",
    "worked_example",
    "practice_problem",
    "check_question",
    "model_answer",
    "rubric_prompt",
    "orientation_segment",
)

artifact_stance = _enum(
    "artifact_stance", "formal", "intuitive", "applied", "historical", "default"
)

mastery_event_kind = _enum(
    "mastery_event_kind",
    "lecture_check_correct",
    "lecture_check_incorrect",
    "tutorial_turn_success",
    "tutorial_turn_stuck",
    "tutorial_turn_recovery",
    "practice_correct",
    "practice_incorrect",
    "practice_partial",
    "assessment_scored",
    "review_correct",
    "review_incorrect",
    "manual_adjustment",
    "decay_refresh",
)

journal_status = _enum("journal_status", "open", "partial", "resolved", "archived")

journal_event_kind = _enum(
    "journal_event_kind",
    "created",
    "revisited",
    "partially_addressed",
    "resolved",
    "reopened",
    "archived",
    "hypothesis_updated",
    "learner_note_added",
)

assessment_mode = _enum("assessment_mode", "formative", "summative")

assessment_trigger = _enum(
    "assessment_trigger", "session_close", "learner_initiated", "scheduled", "unit_gate"
)

#: The first six values are learner *work*: something the learner produced in a
#: session. The last two are credentials -- evaluation §12.1's "assessment_pass"
#: and "subject_completion" -- added by migration 0011.
#:
#: Evaluation §12 assumes both already exist ("Backed by data layer §6.10's
#: portfolio_items and signing_keys tables"). Neither did. See
#: DIVERGENCES-EVALUATION (E1): a credential is signed, externally verifiable
#: and issued only by the summative flow, which is a different lifecycle from a
#: proof the learner wrote in a tutorial, and ``studium.eval.credentials``
#: keeps the two apart by kind.
portfolio_item_kind = _enum(
    "portfolio_item_kind",
    "proof",
    "code",
    "prose",
    "derivation",
    "diagram",
    "notebook",
    "assessment_pass",
    "subject_completion",
)

#: Evaluation §5 addition 1. What question one golden dataset answers, which
#: decides which runner executes it: ``agent_output`` invokes an agent,
#: ``retrieval_quality`` invokes the retriever, and the two remaining kinds are
#: specialisations of the first with their own metric sets (§7.1).
golden_dataset_kind = _enum(
    "golden_dataset_kind",
    "agent_output",
    "retrieval_quality",
    "grading_calibration",
    "content_quality",
)

#: ``normalize`` is added by migration 0010. Ingestion §6 runs normalisation as
#: its own stage between extract and chunk, with its own retry policy and its
#: own version stamp on the source -- so it needs its own job row. See
#: DIVERGENCES-INGESTION (I1): the ingestion spec names the stage everywhere
#: but never noticed the enum it writes to has no value for it.
ingestion_job_kind = _enum(
    "ingestion_job_kind",
    "extract_text",
    "normalize",
    "chunk",
    "embed",
    "suggest_concept_mapping",
)

ingestion_job_status = _enum(
    "ingestion_job_status", "pending", "running", "done", "failed", "cancelled"
)

review_flag_source = _enum(
    "review_flag_source",
    "system_confidence",
    "learner_report",
    "tracker_pattern",
    "evaluator_disagreement",
    "random_sample",
)

#: Ingestion §5 addition 1. Why an ingestion-side row landed in front of a
#: reviewer. Deliberately a separate type from ``review_flag_source``: that one
#: says why *generated content* was flagged, and the two vocabularies have no
#: value in common. Merging them would make every consumer of either switch on
#: values that cannot occur in its own table.
ingestion_flag_source = _enum(
    "ingestion_flag_source",
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

review_status = _enum("review_status", "pending", "in_review", "resolved", "dismissed")


#: Every enum type, in creation order. Migration 0001 walks this list; the
#: enum-stability test in §14 snapshots it.
ALL_ENUMS: tuple[PgEnum, ...] = (
    agent_identity,
    session_mode,
    artifact_status,
    license_kind,
    concept_edge_kind,
    concept_source_role,
    chunk_kind,
    artifact_kind,
    artifact_stance,
    mastery_event_kind,
    journal_status,
    journal_event_kind,
    assessment_mode,
    assessment_trigger,
    portfolio_item_kind,
    ingestion_job_kind,
    ingestion_job_status,
    review_flag_source,
    review_status,
    ingestion_flag_source,
    golden_dataset_kind,
)

#: Tables that carry ``updated_at`` and therefore need the trigger. Migration
#: 0001 installs one per table; the §14 schema test asserts the set matches.
TRIGGERED_TABLES: tuple[str, ...] = (
    "users",
    "user_profiles",
    "subjects",
    "concepts",
    "sources",
    "content_artifacts",
    "rubric_criteria",
    "learner_subjects",
    "concept_mastery",
    "learning_sessions",
    "journal_entries",
    "review_cards",
    "cost_ledger",
    "user_budget_caps",
    "ingestion_jobs",
    "content_review_queue",
    "assessment_attempts",
    "subject_metadata",
    # Added in spec v1.1 §6.11: both are regenerable, so both are mutable.
    "session_summaries",
    "retrieval_checks",
    # Ingestion §5 addition 1, migration 0010. A reviewer assigns, resolves and
    # escalates rows in place, so it is mutable and needs the trigger.
    "ingestion_review_queue",
    # Evaluation §5 additions 1 and 2, migration 0011. Both carry updated_at in
    # the spec's DDL and both are re-materialised in place by `studium eval
    # sync` every time the YAML changes, which is precisely the edit the column
    # exists to date.
    "golden_datasets",
    "golden_dataset_entries",
    # E1's addition. A key is retired in place (retired_at set), so it is
    # mutable; the material itself is never updated.
    "signing_keys",
    # Infrastructure §12.4, migration 0012. A hold is released in place, and
    # `released_at` is the edit `updated_at` exists to date. `retention_actions`
    # is deliberately absent: it is append-only, not mutable.
    "retention_holds",
)

#: Tables with ``deleted_at``. §14 asserts each has a partial index that
#: excludes soft-deleted rows.
SOFT_DELETE_TABLES: tuple[str, ...] = (
    "users",
    "subjects",
    "sources",
    "content_artifacts",
)

#: Append-only tables. Migration 0003 revokes UPDATE/DELETE on these from the
#: application role; the retention and erasure jobs run as the owner instead.
#:
#: Later migrations add their own: 0003's ALTER DEFAULT PRIVILEGES grants
#: UPDATE and DELETE on *future* tables to the app role, so a table added after
#: it that belongs here has to revoke for itself. The §14 grants test scans the
#: whole migration history rather than 0003 alone for exactly that reason.
APPEND_ONLY_TABLES: tuple[str, ...] = (
    "mastery_events",
    "journal_events",
    "review_events",
    "agent_traces",
    "audit_log",
    # Infrastructure §12.2, migration 0012. The retention worker's own audit
    # trail. An audit trail the audited process can rewrite answers nothing --
    # and the app role has no reason to touch it at all, since the worker
    # connects as studium_owner like the deletes it records.
    "retention_actions",
)
