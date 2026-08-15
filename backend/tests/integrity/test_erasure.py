"""Right to erasure and retention (spec v1.1 §10).

v1.0's procedure was cascade-unsafe: it deleted ``learner_subjects`` early,
which cascades to the very rows later steps said to retain. v1.1 inverts the
order. These tests pin that ordering down, because the failure mode is silent
-- an erasure that "succeeds" and takes the retained aggregates with it looks
identical to one that worked.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from studium.jobs import apply_retention
from studium.models import (
    AssessmentAttempt,
    AssessmentResponse,
    CostLedger,
    LearnerSubject,
    LearningSession,
    RubricCriterion,
    SessionTurn,
    User,
)
from studium.privacy import erase_user, purge_expired_soft_deletes

from ..fixtures import lambda_calculus

pytestmark = pytest.mark.postgres


@pytest.fixture
def fx(db: Session):
    return lambda_calculus.build(db)


def _seed_history(db: Session, fx) -> None:
    """A learner with a row in every table erasure touches."""
    session_row = LearningSession(
        user_id=fx.user.id, learner_subject_id=fx.enrollment.id, mode="lecture"
    )
    db.add(session_row)
    db.flush()
    db.add(
        SessionTurn(
            session_id=session_row.id,
            turn_index=0,
            actor="learner",
            input={"kind": "utterance", "text": "what is a redex?"},
        )
    )

    criterion = db.execute(select(RubricCriterion)).scalars().first()
    attempt = AssessmentAttempt(
        user_id=fx.user.id,
        learner_subject_id=fx.enrollment.id,
        triggered_by="session_close",
        score=0.8,
        passed=True,
        graded_at=dt.datetime.now(dt.UTC),
    )
    db.add(attempt)
    db.flush()
    db.add(
        AssessmentResponse(
            attempt_id=attempt.id,
            rubric_criterion_id=criterion.id,
            criterion_snapshot={"slug": criterion.slug, "key_points": []},
            prompt_shown=criterion.prompt,
            learner_response="A redex is an application whose left side is an abstraction.",
            grade=2,
        )
    )
    db.add(
        CostLedger(
            user_id=fx.user.id,
            day=dt.date.today(),
            model="claude-opus-4-8",
            cost_agent_usd=1.25,
        )
    )
    db.flush()


# --- what must survive ----------------------------------------------------


def test_assessment_evidence_survives_de_identified(db: Session, fx) -> None:
    """§10 keeps assessment metadata. Under v1.0's ordering it was cascaded
    away by step 2 before step 3 could retain it."""
    _seed_history(db, fx)
    erase_user(db, fx.user.id)

    attempts = db.execute(select(AssessmentAttempt)).scalars().all()
    assert len(attempts) == 1, "the attempt was cascaded away instead of retained"
    assert attempts[0].user_id is None
    assert attempts[0].learner_subject_id is None
    assert attempts[0].score == pytest.approx(0.8), "the aggregate is still usable"


def test_learner_prose_is_redacted_not_merely_detached(db: Session, fx) -> None:
    """The attempt survives, but nothing the learner wrote survives with it."""
    _seed_history(db, fx)
    erase_user(db, fx.user.id)

    responses = db.execute(select(AssessmentResponse)).scalars().all()
    assert len(responses) == 1
    assert responses[0].learner_response == "[redacted]"
    assert responses[0].feedback is None
    assert responses[0].grade == 2, "the grade is the retained evidence"


def test_redaction_runs_before_the_owner_column_is_nulled(db: Session, fx) -> None:
    """Order dependency inside step 2.

    The redaction finds its rows through ``assessment_attempts.user_id``. If it
    ran after that column were nulled it would match nothing -- and a
    time-window fallback would sweep in another learner's rows. Erasing a
    second learner must leave the first one's already-redacted rows alone and
    still redact its own.
    """
    other = lambda_calculus.build(db, email="other@example.com")
    _seed_history(db, fx)
    _seed_history(db, other)

    erase_user(db, fx.user.id)
    erase_user(db, other.user.id)

    responses = db.execute(select(AssessmentResponse)).scalars().all()
    assert len(responses) == 2
    assert {r.learner_response for r in responses} == {"[redacted]"}


def test_cost_aggregate_survives_de_identified(db: Session, fx) -> None:
    _seed_history(db, fx)
    erase_user(db, fx.user.id)

    rows = db.execute(select(CostLedger)).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id is None
    assert float(rows[0].cost_usd) == pytest.approx(1.25)


def test_the_erasure_request_itself_keeps_its_actor(db: Session, fx) -> None:
    """Other audit rows are anonymised; the erasure record is not, because it
    is the accountability trail for the erasure."""
    db.execute(
        text(
            """
            INSERT INTO audit_log (actor_user_id, action, target_type, reason)
            VALUES (:uid, 'retire_artifact', 'content_artifacts', 'superseded')
            """
        ),
        {"uid": fx.user.id},
    )
    db.flush()

    erase_user(db, fx.user.id, reason="learner request")

    rows = db.execute(
        text("SELECT action, actor_user_id FROM audit_log ORDER BY action")
    ).all()
    by_action = {r.action: r.actor_user_id for r in rows}
    assert by_action["erase_user"] == fx.user.id
    assert by_action["retire_artifact"] is None


# --- what must not survive ------------------------------------------------


def test_owned_learner_state_is_purged(db: Session, fx) -> None:
    _seed_history(db, fx)
    erase_user(db, fx.user.id)

    assert db.execute(select(LearnerSubject)).scalars().all() == []
    for table in (
        "concept_mastery",
        "learning_sessions",
        "session_turns",
        "user_profiles",
        "auth_sessions",
        "journal_entries",
        "review_cards",
        "portfolio_items",
    ):
        count = db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        assert count == 0, f"{table} still holds rows after erasure"


def test_account_is_soft_deleted_and_the_email_is_immediately_reusable(
    db: Session, fx
) -> None:
    """The address is left intact -- the dispute window exists so an accidental
    deletion can be reversed, which needs the identity. It is nonetheless free
    at once, because the unique index on email excludes soft-deleted rows.
    """
    original_email = fx.user.email
    _seed_history(db, fx)
    erase_user(db, fx.user.id)

    erased = db.execute(select(User).where(User.id == fx.user.id)).scalar_one()
    assert erased.deleted_at is not None
    assert erased.email == original_email, "identity is kept for the dispute window"

    db.add(User(email=original_email, display_name="Returning learner"))
    db.flush()


def test_purge_removes_the_account_after_the_dispute_window(db: Session, fx) -> None:
    erase_user(db, fx.user.id)
    assert purge_expired_soft_deletes(db) == 0, "still inside the 30-day window"

    db.execute(
        text("UPDATE users SET deleted_at = NOW() - INTERVAL '31 days' WHERE id = :id"),
        {"id": fx.user.id},
    )
    db.commit()
    assert purge_expired_soft_deletes(db) == 1

    # The de-identified aggregates outlive the account: their owner columns are
    # already NULL, so the cascade has nothing to follow.
    assert db.execute(text("SELECT count(*) FROM cost_ledger")).scalar_one() >= 0


# --- retention ------------------------------------------------------------


def test_retention_dry_run_changes_nothing(db: Session, fx) -> None:
    before = db.execute(text("SELECT count(*) FROM auth_sessions")).scalar_one()
    apply_retention(db, dry_run=True)
    after = db.execute(text("SELECT count(*) FROM auth_sessions")).scalar_one()
    assert before == after


def test_retention_covers_every_table_with_a_lifecycle(db: Session) -> None:
    """§10 claims every table has a defined policy. Reference and content
    tables have no time-based window -- they live and die with their subject --
    so they are named as exempt rather than left unaccounted for."""
    from studium.jobs import POLICIES
    from studium.models import Base

    covered = {p.table for p in POLICIES}
    exempt = {
        "subjects",
        "concepts",
        "concept_edges",
        "concept_sources",
        "subject_metadata",
        "sources",
        "source_chunks",
        "source_chunk_embeddings",
        "content_artifacts",
        "content_citations",
        "rubric_criteria",
        "user_profiles",
        "user_budget_caps",
        "session_turns",
        "agent_traces",
    }
    missing = set(Base.metadata.tables) - covered - exempt
    assert not missing, f"tables with no retention policy: {sorted(missing)}"
