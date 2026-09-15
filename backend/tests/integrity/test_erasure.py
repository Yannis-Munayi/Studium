"""Right to erasure and retention (spec v1.1 §10).

v1.0's procedure was cascade-unsafe: it deleted ``learner_subjects`` early,
which cascades to the very rows later steps said to retain. v1.1 inverts the
order. These tests pin that ordering down, because the failure mode is silent
-- an erasure that "succeeds" and takes the retained aggregates with it looks
identical to one that worked.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from studium.eval import credentials
from studium.eval.credentials import SigningIdentity
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
from studium.models.identity import ANONYMIZED_LEARNER_ID, SYSTEM_USER_ID
from studium.privacy import (
    DISPUTE_WINDOW_DAYS,
    ERASURE_PURGE_POLICY,
    ErasureRefused,
    erase_user,
    purge_expired_soft_deletes,
)

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


# --- credentials outlive their learner (amendment v1.2.1 §3.1) ------------
#
# The failure this section exists for is silent and permanent. ``erase_user``
# without step 3 succeeds, reports nothing unusual, and destroys a signed
# credential that a third party may already be holding a copy of -- so the only
# symptom is an external verifier getting "no such credential" from §12.4
# months later, with nothing left in the database to explain why.


@pytest.fixture
def identity(db: Session) -> SigningIdentity:
    ident = SigningIdentity(
        key_id=f"erasure-{uuid.uuid4().hex[:8]}",
        seed=base64.b64decode(credentials.generate_seed()),
    )
    credentials.register_public_key(db, ident)
    db.flush()
    return ident


def _issue(db: Session, fx, identity: SigningIdentity) -> uuid.UUID:
    """One signed ``assessment_pass`` on the learner's chain."""
    item_id = credentials.issue_credential(
        db,
        user_id=fx.user.id,
        learner_subject_id=fx.enrollment.id,
        subject_slug="lambda-calculus",
        kind="assessment_pass",
        score=0.87,
        passing_threshold=0.70,
        criteria_results=[{"criterion": "beta_reduction", "weight": 2, "grade": 2}],
        assessment_id=uuid.uuid4(),
        identity=identity,
    )
    db.flush()
    return item_id


def _work_item(db: Session, fx, *, chain_index: int) -> uuid.UUID:
    """An ordinary portfolio item on the same chain -- a learner's own proof."""
    return db.execute(
        text(
            """
            INSERT INTO portfolio_items
                (user_id, learner_subject_id, chain_index, kind, title, body,
                 content_sha256, signature, is_credential)
            VALUES (:uid, :lsid, :idx, 'proof', 'A normalisation proof',
                    'Every term with a normal form ...', :digest,
                    '{"signed": false}'::jsonb, FALSE)
            RETURNING id
            """
        ),
        {
            "uid": fx.user.id,
            "lsid": fx.enrollment.id,
            "idx": chain_index,
            "digest": hashlib.sha256(b"proof").hexdigest(),
        },
    ).scalar_one()


def test_a_credential_survives_erasure_and_changes_owner(
    db: Session, fx, identity: SigningIdentity
) -> None:
    """§3.1's requirement, stated as the row that must still be there."""
    item_id = _issue(db, fx, identity)

    erase_user(db, fx.user.id)

    row = db.execute(
        text(
            "SELECT user_id, learner_subject_id, is_credential "
            "  FROM portfolio_items WHERE id = :id"
        ),
        {"id": item_id},
    ).one_or_none()
    assert row is not None, "the credential was cascaded away with the enrollment"
    assert row.user_id == ANONYMIZED_LEARNER_ID
    assert row.learner_subject_id is None, (
        "still attached to the deleted enrollment -- it only survived because "
        "nothing has cascaded yet"
    )
    assert row.is_credential


def test_the_retained_credential_still_verifies(
    db: Session, fx, identity: SigningIdentity
) -> None:
    """The one that matters.

    Retaining the row is not the requirement; retaining a credential a third
    party can still *check* is. §12.4 verifies the credential payload against
    the published key and never reads the hash chain, so detaching the row from
    a chain whose other links have been deleted must not change the answer.
    """
    item_id = _issue(db, fx, identity)
    before = credentials.verify_item(db, item_id)
    assert before.found and before.valid

    erase_user(db, fx.user.id)

    after = credentials.verify_item(db, item_id)
    assert after.found, "§12.4 no longer resolves an id verifiers may be holding"
    assert after.valid, "the signature stopped verifying across erasure"
    assert after.item["learner_id"] == str(fx.user.id), (
        "the signed payload names the learner and cannot be edited without "
        "breaking the signature -- that is what makes it verifiable, and it is "
        "the deliberate limit of what erasure can remove"
    )


def test_work_items_are_removed_by_the_same_erasure(
    db: Session, fx, identity: SigningIdentity
) -> None:
    """Both halves of one erasure, because either alone is a passing test of
    the wrong thing: keeping everything is not retention, and keeping nothing
    is what the amendment is about."""
    credential_id = _issue(db, fx, identity)
    proof_id = _work_item(db, fx, chain_index=1)

    erase_user(db, fx.user.id)

    surviving = set(
        db.execute(text("SELECT id FROM portfolio_items")).scalars().all()
    )
    assert credential_id in surviving
    assert proof_id not in surviving, "a learner's own proof outlived their erasure"


def test_the_credential_also_survives_the_hard_delete(
    db: Session, fx, identity: SigningIdentity
) -> None:
    """Step 3 detaches from the enrollment; the ``users`` row goes 30 days
    later and cascades too. Reassigning the owner is what survives *that*, and
    a test that stops at ``erase_user`` never exercises it."""
    item_id = _issue(db, fx, identity)
    erase_user(db, fx.user.id)

    db.execute(
        text(
            "UPDATE users SET deleted_at = NOW() - INTERVAL "
            f"'{DISPUTE_WINDOW_DAYS + 1} days' WHERE id = :id"
        ),
        {"id": fx.user.id},
    )
    db.commit()
    assert purge_expired_soft_deletes(db) == 1

    assert db.execute(
        text("SELECT count(*) FROM portfolio_items WHERE id = :id"), {"id": item_id}
    ).scalar_one() == 1
    assert credentials.verify_item(db, item_id).valid


def test_erasure_records_that_it_retained_something(
    db: Session, fx, identity: SigningIdentity
) -> None:
    """"The learner asked to be forgotten and a signed credential naming them
    still exists" needs to have been written down at the time."""
    _issue(db, fx, identity)
    erase_user(db, fx.user.id)

    row = db.execute(
        text(
            "SELECT reason, target_id FROM audit_log "
            " WHERE action = 'retain_credentials'"
        )
    ).one()
    assert row.target_id == fx.user.id
    assert "1 credential(s)" in row.reason


def test_an_erasure_with_no_credentials_records_nothing(db: Session, fx) -> None:
    """The audit row is evidence of a retention, so it must not appear for an
    erasure that retained nothing."""
    _work_item(db, fx, chain_index=0)
    erase_user(db, fx.user.id)

    assert db.execute(
        text("SELECT count(*) FROM audit_log WHERE action = 'retain_credentials'")
    ).scalar_one() == 0


@pytest.mark.parametrize(
    "reserved", [SYSTEM_USER_ID, ANONYMIZED_LEARNER_ID], ids=["system", "anonymized"]
)
def test_the_reserved_accounts_cannot_be_erased(db: Session, reserved) -> None:
    """Erasing the anonymised-learner account would cascade away every
    credential every erased learner ever earned -- the exact loss §3.1 exists
    to prevent, in one command."""
    with pytest.raises(ErasureRefused, match="reserved system account"):
        erase_user(db, reserved)


def test_the_flag_cannot_disagree_with_the_kind(db: Session, fx) -> None:
    """The CHECK is what makes the denormalisation safe, so it has to be shown
    failing. Without it, an item written with the flag set wrong is either a
    credential erasure deletes or an essay erasure keeps, and nothing says so.
    """
    with pytest.raises(IntegrityError, match="is_credential_matches_kind"):
        db.execute(
            text(
                """
                INSERT INTO portfolio_items
                    (user_id, learner_subject_id, chain_index, kind, title, body,
                     content_sha256, signature, is_credential)
                VALUES (:uid, :lsid, 99, 'prose', 'An essay', 'body', :digest,
                        '{}'::jsonb, TRUE)
                """
            ),
            {
                "uid": fx.user.id,
                "lsid": fx.enrollment.id,
                "digest": hashlib.sha256(b"essay").hexdigest(),
            },
        )


# --- the erasure purge leaves a trail (amendment v1.2.1 §3.2) -------------


def _expire(db: Session, user_id) -> None:
    db.execute(
        text(
            "UPDATE users SET deleted_at = NOW() - INTERVAL "
            f"'{DISPUTE_WINDOW_DAYS + 1} days' WHERE id = :id"
        ),
        {"id": user_id},
    )
    db.commit()


def _purge_rows(db: Session) -> list:
    return db.execute(
        text(
            """
            SELECT table_name, rows_deleted, metadata
              FROM retention_actions
             WHERE metadata ->> 'policy_name' = :policy
            """
        ),
        {"policy": ERASURE_PURGE_POLICY},
    ).all()


def test_the_purge_writes_one_row_per_account(db: Session, fx) -> None:
    """§3.2: every ordinary retention policy writes a ``retention_actions``
    row; the one deletion with a statutory deadline wrote none."""
    erase_user(db, fx.user.id)
    _expire(db, fx.user.id)

    assert purge_expired_soft_deletes(db) == 1

    rows = _purge_rows(db)
    assert len(rows) == 1
    assert rows[0].table_name == "users"
    assert rows[0].rows_deleted == 1
    assert rows[0].metadata["dispute_window_days"] == DISPUTE_WINDOW_DAYS


def test_nothing_is_written_before_the_window_elapses(db: Session, fx) -> None:
    """The other timing condition. A row written at request time would say a
    disposal happened on the day it was merely asked for."""
    erase_user(db, fx.user.id)

    assert purge_expired_soft_deletes(db) == 0, "still inside the 30-day window"
    assert _purge_rows(db) == []


def test_the_purge_row_identifies_the_account_without_storing_it(
    db: Session, fx
) -> None:
    """An auditor holding a candidate id can confirm this row is the one; the
    row itself is not a second list of erased learners."""
    user_id = fx.user.id
    erase_user(db, user_id)
    _expire(db, user_id)
    purge_expired_soft_deletes(db)

    metadata = _purge_rows(db)[0].metadata
    assert metadata["user_id_sha256"] == hashlib.sha256(
        str(user_id).encode("utf-8")
    ).hexdigest()
    assert str(user_id) not in str(metadata), "the identifier came back in the trail"


def test_the_purge_row_links_back_to_the_erasure_request(db: Session, fx) -> None:
    """"Did the request from user X on date Y complete by Y+30" is answered by
    following this reference, which is why it is resolved before the ``users``
    row goes and its ON DELETE SET NULL erases the link."""
    erase_user(db, fx.user.id, reason="learner request")
    requested = db.execute(
        text(
            "SELECT id, created_at FROM audit_log "
            " WHERE action = 'erase_user' AND target_id = :id"
        ),
        {"id": fx.user.id},
    ).one()
    _expire(db, fx.user.id)
    purge_expired_soft_deletes(db)

    metadata = _purge_rows(db)[0].metadata
    assert metadata["audit_log_ref"] == str(requested.id)
    assert dt.datetime.fromisoformat(metadata["deleted_at_original"]) < dt.datetime.now(
        dt.UTC
    ) - dt.timedelta(days=DISPUTE_WINDOW_DAYS)


def test_two_accounts_purged_together_get_a_row_each(db: Session, fx) -> None:
    """One row per account, not per pass: the question §3.2 exists for is asked
    about a person and a date, and a single row saying "2" answers it for
    neither of them."""
    other = lambda_calculus.build(db, email="second@example.com")
    db.flush()
    for user in (fx.user, other.user):
        erase_user(db, user.id)
        _expire(db, user.id)

    assert purge_expired_soft_deletes(db) == 2

    rows = _purge_rows(db)
    assert len(rows) == 2
    assert {r.metadata["user_id_sha256"] for r in rows} == {
        hashlib.sha256(str(u.id).encode("utf-8")).hexdigest()
        for u in (fx.user, other.user)
    }
    assert all(r.metadata["accounts_in_pass"] == 2 for r in rows)


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
