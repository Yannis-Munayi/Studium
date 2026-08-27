"""Enum stability (spec §14).

"Enum values are stable (a test that snapshots enum values and fails on
unreviewed changes)."

Renaming or removing a value is a two-step deploy (§13) because ALTER TYPE ...
RENAME VALUE requires no running query to reference the old value. Adding is
safe. This snapshot makes the difference impossible to miss in review: adding
a value means adding a line here, removing one means you had to think about it.
"""

from __future__ import annotations

import pytest

from studium.models import ALL_ENUMS

SNAPSHOT: dict[str, tuple[str, ...]] = {
    "agent_identity": (
        "learner",
        "orchestrator",
        "curator",
        "lecturer",
        "tutor",
        "evaluator",
        "confusion_tracker",
        "reviewer",
        "system",
    ),
    "session_mode": (
        "orientation",
        "lecture",
        "tutorial",
        "lab",
        "review",
        "office_hours",
        "summative_assessment",
    ),
    "artifact_status": ("draft", "reviewed", "active", "retired"),
    "license_kind": (
        "public_domain",
        "cc_by",
        "cc_by_sa",
        "cc_by_nc",
        "user_uploaded",
        "permission_granted",
        "fair_use",
    ),
    "concept_edge_kind": (
        "prerequisite",
        "dependency",
        "generalization",
        "application",
        "related",
    ),
    "concept_source_role": (
        "canonical_definition",
        "primary_exposition",
        "worked_example",
        "exercise",
        "historical",
        "alternative_stance",
    ),
    # Added by migration 0009 for the retrieval subsystem (retrieval §5).
    "chunk_kind": (
        "body",
        "heading",
        "code",
        "math",
        "figure_caption",
        "exercise",
        "reference",
    ),
    "artifact_kind": (
        "lecture_segment",
        "tutorial_seed",
        "worked_example",
        "practice_problem",
        "check_question",
        "model_answer",
        "rubric_prompt",
        "orientation_segment",
    ),
    "artifact_stance": ("formal", "intuitive", "applied", "historical", "default"),
    "mastery_event_kind": (
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
    ),
    "journal_status": ("open", "partial", "resolved", "archived"),
    "journal_event_kind": (
        "created",
        "revisited",
        "partially_addressed",
        "resolved",
        "reopened",
        "archived",
        "hypothesis_updated",
        "learner_note_added",
    ),
    "assessment_mode": ("formative", "summative"),
    "assessment_trigger": (
        "session_close",
        "learner_initiated",
        "scheduled",
        "unit_gate",
    ),
    # The last two are added by migration 0011. Evaluation §12.1 credentials a
    # passed assessment and a completed subject; the first six values are
    # learner *work* and none of them could carry a credential.
    # DIVERGENCES-EVALUATION (E1).
    "portfolio_item_kind": (
        "proof",
        "code",
        "prose",
        "derivation",
        "diagram",
        "notebook",
        "assessment_pass",
        "subject_completion",
    ),
    # 'normalize' added by migration 0010: ingestion §6.4 runs normalisation as
    # its own job, and the enum it writes to had no value for it.
    "ingestion_job_kind": (
        "extract_text",
        "normalize",
        "chunk",
        "embed",
        "suggest_concept_mapping",
    ),
    "ingestion_job_status": ("pending", "running", "done", "failed", "cancelled"),
    "review_flag_source": (
        "system_confidence",
        "learner_report",
        "tracker_pattern",
        "evaluator_disagreement",
        "random_sample",
    ),
    "review_status": ("pending", "in_review", "resolved", "dismissed"),
    # Added by migration 0010 for the ingestion subsystem (ingestion §5
    # addition 1). Separate from review_flag_source: that enum says why
    # generated content was flagged, this one why an ingestion-side row was,
    # and the two share no value.
    "ingestion_flag_source": (
        "extractor_failure",
        "normalizer_warning",
        "embedding_failure",
        "chunk_ambiguous_type",
        "license_pending",
        "license_conflict",
        "concept_source_conflict",
        "graph_validation_error",
        "rubric_validation_error",
    ),
    # Added by migration 0011 for the evaluation subsystem (evaluation §5
    # addition 1). What question one golden dataset answers, which decides
    # which runner executes it.
    "golden_dataset_kind": (
        "agent_output",
        "retrieval_quality",
        "grading_calibration",
        "content_quality",
    ),
}


def test_every_enum_is_snapshotted() -> None:
    defined = {e.name for e in ALL_ENUMS}
    assert defined == set(SNAPSHOT), (
        f"enum set changed: added {defined - set(SNAPSHOT)}, "
        f"removed {set(SNAPSHOT) - defined}"
    )


@pytest.mark.parametrize("enum", ALL_ENUMS, ids=lambda e: e.name)
def test_enum_values_unchanged(enum) -> None:
    expected = SNAPSHOT[enum.name]
    actual = tuple(enum.enums)
    assert actual == expected, (
        f"{enum.name} changed.\n  was: {expected}\n  now: {actual}\n"
        f"Adding a value is safe. Removing or renaming needs the two-step "
        f"deploy in spec §13 -- and an update to this snapshot."
    )


def test_mastery_event_kinds_are_classified() -> None:
    """Every mastery_event_kind must be handled by the BKT update.

    A new kind that no branch recognises would raise at runtime on the first
    piece of evidence that used it.
    """
    from studium.mastery import (
        ADMINISTRATIVE_KINDS,
        CORRECT_KINDS,
        GRADED_KINDS,
        INCORRECT_KINDS,
    )

    classified = CORRECT_KINDS | INCORRECT_KINDS | GRADED_KINDS | ADMINISTRATIVE_KINDS
    declared = set(SNAPSHOT["mastery_event_kind"])
    assert declared == classified, (
        f"unclassified mastery event kinds: {declared - classified}; "
        f"unknown kinds referenced: {classified - declared}"
    )
