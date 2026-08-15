"""The five-source cost roll-up (spec v1.1 §6.12, §8).

v1.0 summed only ``agent_traces``, so content generation, ingestion,
summarisation and grading never reached the number budget enforcement reads.
At MVP those are plausibly the larger share, because content is generated once
and read many times -- which makes under-counting here the same thing as
under-counting the risk the spec calls most likely to end the project.

Each source gets a test that it lands in its own column and is attributed to
the right owner.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from studium import cost
from studium.jobs import roll_up_day
from studium.models import (
    AssessmentAttempt,
    ContentArtifact,
    CostLedger,
    IngestionJob,
    LearningSession,
    SessionSummary,
    SessionTurn,
)
from studium.models.identity import SYSTEM_USER_ID

from ..fixtures import lambda_calculus

pytestmark = pytest.mark.postgres

MODEL = "claude-opus-4-8"


@pytest.fixture
def fx(db: Session):
    return lambda_calculus.build(db)


@pytest.fixture
def session_row(db: Session, fx) -> LearningSession:
    row = LearningSession(
        user_id=fx.user.id, learner_subject_id=fx.enrollment.id, mode="lecture"
    )
    db.add(row)
    db.flush()
    return row


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def _add_trace(db: Session, session_row, user_id, cost_usd: float, **tokens) -> None:
    turn = SessionTurn(
        session_id=session_row.id,
        turn_index=db.execute(
            text(
                "SELECT COALESCE(MAX(turn_index), -1) + 1 FROM session_turns "
                "WHERE session_id = :s"
            ),
            {"s": session_row.id},
        ).scalar_one(),
        actor="tutor",
        input={},
    )
    db.add(turn)
    db.flush()
    db.execute(
        text(
            """
            INSERT INTO agent_traces (
                session_turn_id, user_id, agent, model, prompt_messages,
                system_prompt_hash, latency_ms, cost_usd,
                tokens_in, tokens_out, cache_read_tokens,
                cache_write_5m_tokens, cache_write_1h_tokens
            ) VALUES (
                :turn, :user, 'tutor', :model, '[]'::jsonb, repeat('a', 64),
                100, :cost, :tin, :tout, :cread, :c5m, :c1h
            )
            """
        ),
        {
            "turn": turn.id,
            "user": user_id,
            "model": MODEL,
            "cost": cost_usd,
            "tin": tokens.get("tokens_in", 0),
            "tout": tokens.get("tokens_out", 0),
            "cread": tokens.get("cache_read_tokens", 0),
            "c5m": tokens.get("cache_write_5m_tokens", 0),
            "c1h": tokens.get("cache_write_1h_tokens", 0),
        },
    )
    db.flush()


def _ledger(db: Session, user_id) -> CostLedger | None:
    return db.execute(
        select(CostLedger).where(CostLedger.user_id == user_id)
    ).scalars().first()


# --- the generated total --------------------------------------------------


def test_total_is_the_sum_of_its_parts(db: Session, fx) -> None:
    db.add(
        CostLedger(
            user_id=fx.user.id,
            day=_today(),
            model=MODEL,
            cost_agent_usd=1,
            cost_content_usd=2,
            cost_ingestion_usd=4,
            cost_summary_usd=8,
            cost_grading_usd=16,
        )
    )
    db.flush()
    db.expire_all()
    assert float(_ledger(db, fx.user.id).cost_usd) == pytest.approx(31.0)


def test_the_total_cannot_be_written_directly(db: Session, fx) -> None:
    """A generated column is the guard that keeps the total honest: an
    ON CONFLICT update that tried to set it is rejected outright."""
    db.add(CostLedger(user_id=fx.user.id, day=_today(), model=MODEL, cost_agent_usd=1))
    db.flush()
    with pytest.raises(Exception) as exc:
        db.execute(text("UPDATE cost_ledger SET cost_usd = 999"))
        db.flush()
    assert "generated" in str(exc.value).lower()


# --- per-source attribution ----------------------------------------------


def test_agent_traces_land_in_the_agent_column_with_tokens(
    db: Session, fx, session_row
) -> None:
    _add_trace(
        db,
        session_row,
        fx.user.id,
        0.75,
        tokens_in=100,
        tokens_out=50,
        cache_write_5m_tokens=10,
        cache_write_1h_tokens=7,
    )
    roll_up_day(db, _today())

    row = _ledger(db, fx.user.id)
    assert float(row.cost_agent_usd) == pytest.approx(0.75)
    assert row.tokens_in == 100
    # The TTL split is preserved rather than collapsed -- the two price
    # differently, so one column could not re-derive the cost.
    assert row.cache_write_5m_tokens == 10
    assert row.cache_write_1h_tokens == 7


def test_content_cost_follows_the_triggering_session(
    db: Session, fx, session_row
) -> None:
    db.add(
        ContentArtifact(
            concept_id=fx.concept_id("syntax"),
            kind="lecture_segment",
            body="...",
            generated_by="lecturer",
            model=MODEL,
            cost_usd=0.4,
            generated_for_session_id=session_row.id,
            meta={"segment_index": 0},
        )
    )
    db.flush()
    roll_up_day(db, _today())

    assert float(_ledger(db, fx.user.id).cost_content_usd) == pytest.approx(0.4)


def test_pre_generated_content_goes_to_the_system_account(db: Session, fx) -> None:
    """Curator pre-generation has no requesting learner. It must not land on a
    learner's budget, and it must not land on NULL either -- NULL means
    post-erasure aggregate."""
    db.add(
        ContentArtifact(
            concept_id=fx.concept_id("syntax"),
            kind="lecture_segment",
            body="...",
            generated_by="curator",
            model=MODEL,
            cost_usd=2.5,
            generated_for_session_id=None,
            meta={"segment_index": 1},
        )
    )
    db.flush()
    roll_up_day(db, _today())

    assert _ledger(db, fx.user.id) is None, "no learner should have been charged"
    system = _ledger(db, SYSTEM_USER_ID)
    assert system is not None and float(system.cost_content_usd) == pytest.approx(2.5)

    unowned = db.execute(
        select(CostLedger).where(CostLedger.user_id.is_(None))
    ).scalars().all()
    assert unowned == [], "NULL owner is reserved for post-erasure aggregates"


def test_unattributed_content_is_counted_not_absorbed(db: Session, fx) -> None:
    """The one silent failure mode in the v1.1 gap: a content pipeline written
    from §6.4 alone never sets the session, so every artifact books to the
    system account and nothing complains. The job reports the count so the
    misattribution is at least visible."""
    for index, session_id in enumerate([None, None]):
        db.add(
            ContentArtifact(
                concept_id=fx.concept_id("syntax"),
                kind="lecture_segment",
                body="...",
                generated_by="curator",
                model=MODEL,
                cost_usd=1.0,
                generated_for_session_id=session_id,
                meta={"segment_index": 100 + index},
            )
        )
    db.flush()

    counts = roll_up_day(db, _today())
    assert counts["unattributed_content"] == 2


def test_ingestion_cost_follows_the_uploader(db: Session, fx) -> None:
    db.execute(
        text("UPDATE sources SET uploaded_by = :u WHERE id = :s"),
        {"u": fx.user.id, "s": fx.source.id},
    )
    db.add(
        IngestionJob(
            source_id=fx.source.id,
            kind="embed",
            status="done",
            cost_usd=0.9,
            finished_at=dt.datetime.now(dt.UTC),
        )
    )
    db.flush()
    roll_up_day(db, _today())

    assert float(_ledger(db, fx.user.id).cost_ingestion_usd) == pytest.approx(0.9)


def test_corpus_import_cost_goes_to_the_system_account(db: Session, fx) -> None:
    db.add(
        IngestionJob(
            source_id=fx.source.id,  # fixture source has no uploaded_by
            kind="chunk",
            status="done",
            cost_usd=1.1,
            finished_at=dt.datetime.now(dt.UTC),
        )
    )
    db.flush()
    roll_up_day(db, _today())

    assert float(_ledger(db, SYSTEM_USER_ID).cost_ingestion_usd) == pytest.approx(1.1)


def test_summary_and_grading_cost_follow_their_owners(
    db: Session, fx, session_row
) -> None:
    db.add(
        SessionSummary(session_id=session_row.id, summary="covered redexes", cost_usd=0.2)
    )
    db.add(
        AssessmentAttempt(
            user_id=fx.user.id,
            learner_subject_id=fx.enrollment.id,
            triggered_by="session_close",
            score=0.9,
            passed=True,
            graded_at=dt.datetime.now(dt.UTC),
            grading_cost_usd=0.3,
        )
    )
    db.flush()
    roll_up_day(db, _today())

    rows = db.execute(
        select(CostLedger).where(CostLedger.user_id == fx.user.id)
    ).scalars().all()
    assert sum(float(r.cost_summary_usd) for r in rows) == pytest.approx(0.2)
    assert sum(float(r.cost_grading_usd) for r in rows) == pytest.approx(0.3)


def test_roll_up_is_idempotent(db: Session, fx, session_row) -> None:
    """Re-running a day recomputes from source rather than accumulating, so a
    failed or partial run is fixed by running it again."""
    _add_trace(db, session_row, fx.user.id, 1.0)
    roll_up_day(db, _today())
    roll_up_day(db, _today())
    roll_up_day(db, _today())

    assert float(_ledger(db, fx.user.id).cost_agent_usd) == pytest.approx(1.0)


# --- live spend -----------------------------------------------------------


def test_today_spent_sees_cost_the_roll_up_has_not_yet_seen(
    db: Session, fx, session_row
) -> None:
    """The whole point of the helper: a long session must not be able to
    outrun the daily job."""
    _add_trace(db, session_row, fx.user.id, 2.0)
    db.add(
        SessionSummary(session_id=session_row.id, summary="s", cost_usd=0.5)
    )
    db.flush()

    spent = cost.today_spent(db, fx.user.id)
    assert spent["agent"] == pytest.approx(2.0)
    assert spent["summary"] == pytest.approx(0.5)
    assert spent["total"] == pytest.approx(2.5)


def test_budget_blocks_on_live_spend_alone(db: Session, fx, session_row) -> None:
    """Daily hard cap is 8.00; nothing has been rolled up yet."""
    _add_trace(db, session_row, fx.user.id, 9.0)

    status = cost.budget_status(db, fx.user.id)
    assert status.today_usd == pytest.approx(9.0)
    assert status.blocked
    assert status.warn


def test_budget_reads_both_cap_tiers(db: Session, fx) -> None:
    """v1.0's query selected only the hard caps, so the soft-cap warning
    §6.12 describes could never fire."""
    status = cost.budget_status(db, fx.user.id)
    assert status.daily_soft_usd == pytest.approx(5.00)
    assert status.daily_hard_usd == pytest.approx(8.00)
    assert not status.blocked and not status.warn


def test_budget_does_not_double_count_today(db: Session, fx, session_row) -> None:
    """Today's ledger rows are excluded from the monthly sum and taken from the
    live sources instead -- counting both would report double."""
    _add_trace(db, session_row, fx.user.id, 3.0)
    roll_up_day(db, _today())

    status = cost.budget_status(db, fx.user.id)
    assert status.today_usd == pytest.approx(3.0)
    assert status.month_usd == pytest.approx(3.0)


def test_reconcile_reports_only_provider_billable_cost(
    db: Session, fx, session_row
) -> None:
    """The four internal categories are attributions of the same calls or of
    non-metered work; including them would double-count against an invoice."""
    _add_trace(db, session_row, fx.user.id, 1.0, tokens_in=10, tokens_out=5)
    db.add(SessionSummary(session_id=session_row.id, summary="s", cost_usd=99.0))
    db.flush()
    roll_up_day(db, _today())

    from studium.jobs import reconcile

    report = reconcile(db, _today())
    assert report[MODEL]["cost_usd"] == pytest.approx(1.0)
    assert all("pipeline:" not in model for model in report)
