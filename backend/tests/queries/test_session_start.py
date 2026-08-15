"""Query-shape tests (spec §14, "Query-shape tests").

"The five session-start queries run under 10ms each on a seeded dataset of 100
concepts, 1000 turns, 10 sessions" and "EXPLAIN ANALYZE output for each of the
query patterns in §8 is snapshotted; changes force reviewer attention."

The timing assertion is deliberately generous here -- wall-clock on a laptop is
not a stable gate. What is asserted strictly is the *plan shape*: a sequential
scan creeping into a session-start query is the regression that matters, and it
shows up long before the milliseconds do.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from studium import queries
from studium.models import Concept, LearningSession, SessionTurn

from ..fixtures import lambda_calculus

pytestmark = pytest.mark.postgres

#: Loose enough not to flake, tight enough to catch a missing index.
BUDGET_MS = 50.0


@pytest.fixture
def seeded(db: Session):
    """The §14 dataset: 100 concepts, 10 sessions, 1000 turns."""
    fx = lambda_calculus.build(db)

    for i in range(len(lambda_calculus.CONCEPTS), 100):
        db.add(
            Concept(
                subject_id=fx.subject.id,
                slug=f"filler-{i}",
                title=f"Filler concept {i}",
                position=i,
            )
        )
    db.flush()

    for _ in range(10):
        session_row = LearningSession(
            user_id=fx.user.id, learner_subject_id=fx.enrollment.id, mode="tutorial"
        )
        db.add(session_row)
        db.flush()
        for t in range(100):
            db.add(
                SessionTurn(
                    session_id=session_row.id,
                    turn_index=t,
                    actor="learner" if t % 2 else "tutor",
                    input={"kind": "utterance", "text": f"turn {t}"},
                )
            )
    db.flush()
    db.execute(text("ANALYZE"))
    return fx


def _timed(fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, (time.perf_counter() - start) * 1000


def test_session_start_queries_are_fast(db: Session, seeded) -> None:
    fx = seeded
    calls = [
        ("learner + profile", queries.learner_with_profile, (db, fx.user.id)),
        ("active enrollments", queries.active_enrollments, (db, fx.user.id)),
        ("open journal", queries.open_journal_entries, (db, fx.enrollment.id)),
        ("due review cards", queries.due_review_cards, (db, fx.enrollment.id)),
        ("prior summary", queries.prior_session_summary, (db, fx.enrollment.id)),
    ]
    slow = []
    for label, fn, args in calls:
        _, elapsed = _timed(fn, *args)
        if elapsed > BUDGET_MS:
            slow.append(f"{label}: {elapsed:.1f}ms")
    assert not slow, f"session-start queries over {BUDGET_MS}ms: {slow}"


@pytest.mark.parametrize(
    ("label", "sql", "params"),
    [
        (
            "open journal entries",
            """
            SELECT * FROM journal_entries
             WHERE learner_subject_id = :ls
               AND status IN ('open', 'partial')
             ORDER BY last_touched_at DESC LIMIT 20
            """,
            {"ls": None},
        ),
        (
            "due review cards",
            """
            SELECT rc.* FROM review_cards rc
             WHERE rc.learner_subject_id = :ls
               AND rc.due_at <= NOW() AND NOT rc.suspended
             ORDER BY rc.due_at LIMIT 10
            """,
            {"ls": None},
        ),
        (
            "turns by concept",
            """
            SELECT * FROM session_turns
             WHERE concept_id = :cid ORDER BY created_at DESC LIMIT 20
            """,
            {"cid": None},
        ),
    ],
)
def test_no_sequential_scan_on_indexed_paths(
    db: Session, seeded, label: str, sql: str, params: dict
) -> None:
    """A seq scan here means an index named in §7 stopped being used."""
    bound = dict(params)
    if "ls" in bound:
        bound["ls"] = seeded.enrollment.id
    if "cid" in bound:
        bound["cid"] = seeded.concept_id("syntax")

    plan = db.execute(text(f"EXPLAIN (FORMAT JSON) {sql}"), bound).scalar_one()
    rendered = str(plan)
    assert "Seq Scan" not in rendered, f"{label} fell back to a sequential scan:\n{rendered}"


def test_turn_index_allocation_is_gapless(db: Session, seeded) -> None:
    """Turn indices come from the database, not an in-process counter."""
    session_row = LearningSession(
        user_id=seeded.user.id, learner_subject_id=seeded.enrollment.id, mode="lab"
    )
    db.add(session_row)
    db.flush()

    for expected in range(5):
        index = queries.next_turn_index(db, session_row.id)
        assert index == expected
        db.add(
            SessionTurn(
                session_id=session_row.id, turn_index=index, actor="learner", input={}
            )
        )
        db.flush()


# Budget-check behaviour moved to tests/integrity/test_cost_rollup.py, which
# exercises all five cost sources rather than agent traces alone.


def test_session_cost_trigger_accumulates(db: Session, seeded) -> None:
    """learning_sessions.total_cost_usd is maintained from agent_traces, not
    session_turns -- the latter has no cost column."""
    session_row = LearningSession(
        user_id=seeded.user.id, learner_subject_id=seeded.enrollment.id, mode="lecture"
    )
    db.add(session_row)
    db.flush()
    for i in range(3):
        turn = SessionTurn(
            session_id=session_row.id, turn_index=i, actor="tutor", input={}
        )
        db.add(turn)
        db.flush()
        db.execute(
            text(
                """
                INSERT INTO agent_traces (
                    session_turn_id, user_id, agent, model, prompt_messages,
                    system_prompt_hash, latency_ms, cost_usd
                ) VALUES (
                    :turn, :user, 'tutor', 'claude-opus-4-8', '[]'::jsonb,
                    repeat('b', 64), 100, 0.50
                )
                """
            ),
            {"turn": turn.id, "user": seeded.user.id},
        )
    db.flush()

    total = db.execute(
        text("SELECT total_cost_usd FROM learning_sessions WHERE id = :id"),
        {"id": session_row.id},
    ).scalar_one()
    assert float(total) == pytest.approx(1.50)
