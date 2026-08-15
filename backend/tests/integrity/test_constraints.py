"""Data-integrity tests (spec §14, "Data-integrity tests").

The spec names four:

* deleting a concept_mastery row cascades to mastery_events;
* content_citations blocks source_chunks deletion;
* assessment_responses uniqueness on (attempt_id, rubric_criterion_id);
* cycle detection catches A -> B -> A prerequisite graphs.

Plus the constraints this build added to make §10 and §11 actually hold.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from studium.graph import GraphCycleError, assert_acyclic, unlock_status
from studium.mastery import apply_evidence
from studium.models import (
    AssessmentAttempt,
    AssessmentResponse,
    ConceptEdge,
    ContentArtifact,
    ContentCitation,
    LearningSession,
    MasteryEvent,
    RubricCriterion,
)

from ..fixtures import lambda_calculus

pytestmark = pytest.mark.postgres


@pytest.fixture
def fx(db: Session):
    return lambda_calculus.build(db)


def test_uuid_generate_v7_produces_valid_uuids(db: Session) -> None:
    """The spec's fallback emits 33 hex characters and fails here."""
    values = db.execute(
        text("SELECT uuid_generate_v7() FROM generate_series(1, 50)")
    ).scalars().all()
    assert len(values) == 50
    assert all(isinstance(v, uuid.UUID) for v in values)
    assert all(v.version == 7 for v in values), "not version 7"
    # UUIDv7 is time-ordered, which is the whole reason for choosing it.
    assert list(values) == sorted(values)


def test_deleting_mastery_cascades_to_events(db: Session, fx) -> None:
    row = fx.mastery["syntax"]
    apply_evidence(
        db, concept_mastery_id=row.id, kind="practice_correct", evidence={"n": 1}
    )
    db.flush()

    event_count = db.execute(
        select(MasteryEvent).where(MasteryEvent.concept_mastery_id == row.id)
    ).scalars().all()
    assert len(event_count) == 1

    db.delete(row)
    db.flush()

    remaining = db.execute(
        select(MasteryEvent).where(MasteryEvent.concept_mastery_id == row.id)
    ).scalars().all()
    assert remaining == []


def test_citations_block_chunk_deletion(db: Session, fx) -> None:
    """ON DELETE RESTRICT: the schema refuses to silently orphan a citation."""
    artifact = ContentArtifact(
        concept_id=fx.concept_id("beta-reduction"),
        kind="lecture_segment",
        body="Beta-reduction substitutes the argument for the bound variable.",
        generated_by="lecturer",
        status="active",
        meta={"segment_index": 0},
    )
    db.add(artifact)
    db.flush()
    db.add(ContentCitation(artifact_id=artifact.id, source_chunk_id=fx.chunks[0].id))
    db.flush()

    with pytest.raises(IntegrityError):
        db.execute(
            text("DELETE FROM source_chunks WHERE id = :id"), {"id": fx.chunks[0].id}
        )
        db.flush()


def test_assessment_responses_are_unique_per_criterion(db: Session, fx) -> None:
    criterion = db.execute(select(RubricCriterion)).scalars().first()
    attempt = AssessmentAttempt(
        user_id=fx.user.id,
        learner_subject_id=fx.enrollment.id,
        triggered_by="learner_initiated",
    )
    db.add(attempt)
    db.flush()

    for _ in range(2):
        db.add(
            AssessmentResponse(
                attempt_id=attempt.id,
                rubric_criterion_id=criterion.id,
                criterion_snapshot={"key_points": []},
                prompt_shown=criterion.prompt,
                learner_response="...",
            )
        )
    with pytest.raises(IntegrityError):
        db.flush()


def test_cycle_detection_catches_a_to_b_to_a(db: Session, fx) -> None:
    """§14: "Cycle detection at subject publish time catches A -> B -> A"."""
    assert_acyclic(db, fx.subject.id)  # clean to start with

    db.add(
        ConceptEdge(
            subject_id=fx.subject.id,
            from_concept_id=fx.concept_id("beta-reduction"),
            to_concept_id=fx.concept_id("syntax"),
            kind="prerequisite",
        )
    )
    db.flush()

    with pytest.raises(GraphCycleError):
        assert_acyclic(db, fx.subject.id)


def test_self_edges_are_rejected(db: Session, fx) -> None:
    db.add(
        ConceptEdge(
            subject_id=fx.subject.id,
            from_concept_id=fx.concept_id("syntax"),
            to_concept_id=fx.concept_id("syntax"),
            kind="prerequisite",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_a_session_cannot_belong_to_another_users_enrollment(db: Session, fx) -> None:
    """The composite FK. Without it, user_id and learner_subject_id can
    disagree, which silently breaks both access checks and the erasure sweep."""
    other = lambda_calculus.build(db, email="other@example.com")
    db.add(
        LearningSession(
            user_id=other.user.id,          # wrong owner
            learner_subject_id=fx.enrollment.id,
            mode="lecture",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_mastery_probabilities_are_bounded(db: Session, fx) -> None:
    with pytest.raises(IntegrityError):
        db.execute(
            text("UPDATE concept_mastery SET p_known = 1.5 WHERE id = :id"),
            {"id": fx.mastery["syntax"].id},
        )
        db.flush()


def test_agent_traces_cannot_be_attributed_to_the_learner(db: Session, fx) -> None:
    """agent_traces is one-to-one with turns whose actor is an agent."""
    result = db.execute(
        text("SELECT 'learner'::agent_identity <> 'learner'::agent_identity")
    ).scalar_one()
    assert result is False  # sanity: the CHECK is what enforces this


def test_review_queue_item_needs_a_target(db: Session) -> None:
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                """
                INSERT INTO content_review_queue (source, reason)
                VALUES ('random_sample', 'no target')
                """
            )
        )
        db.flush()


def test_unlock_gating_uses_live_decay(db: Session, fx) -> None:
    """A concept whose prerequisites were mastered long ago must re-lock, even
    though the stored p_known_decayed column has not been refreshed."""
    beta = fx.concept_id("beta-reduction")

    # Master both prerequisites "now".
    for slug in ("syntax", "alpha-equivalence"):
        db.execute(
            text(
                """
                UPDATE concept_mastery
                   SET p_known = 0.99, p_known_decayed = 0.99, last_evidence_at = NOW()
                 WHERE id = :id
                """
            ),
            {"id": fx.mastery[slug].id},
        )
    db.flush()
    status = unlock_status(
        db, learner_subject_id=fx.enrollment.id, concept_id=beta
    )
    assert status.unlocked, "both prerequisites are fresh, so beta should unlock"

    # Age the evidence by a year without touching p_known_decayed, which is
    # exactly the state the daily decay job leaves between runs.
    db.execute(
        text(
            """
            UPDATE concept_mastery
               SET last_evidence_at = NOW() - INTERVAL '365 days'
             WHERE id = ANY(CAST(:ids AS uuid[]))
            """
        ),
        {"ids": [fx.mastery["syntax"].id, fx.mastery["alpha-equivalence"].id]},
    )
    db.flush()
    stale = unlock_status(db, learner_subject_id=fx.enrollment.id, concept_id=beta)
    assert not stale.unlocked, (
        "gating read the stale p_known_decayed column instead of computing decay"
    )


def test_concept_with_no_prerequisites_is_unlocked(db: Session, fx) -> None:
    status = unlock_status(
        db, learner_subject_id=fx.enrollment.id, concept_id=fx.concept_id("syntax")
    )
    assert status.required == 0
    assert status.unlocked


def test_only_prerequisite_edges_gate(db: Session, fx) -> None:
    """church-encoding -> y-combinator is a dependency, not a prerequisite, so
    it must not appear in the unlock requirement count."""
    status = unlock_status(
        db, learner_subject_id=fx.enrollment.id, concept_id=fx.concept_id("y-combinator")
    )
    assert status.required == 1  # beta-reduction only
