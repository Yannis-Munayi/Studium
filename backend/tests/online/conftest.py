"""Gating and fixtures for the Tier 2/3 online tiers (agent runtime §23).

Tier 2 "requires live database and Anthropic API access". Both are gated
explicitly and skip with a reason naming what is missing, rather than failing --
a red suite that everyone learns to ignore is worse than a skipped one that
says why.

The Anthropic gate is opt-in twice over: a key must be present *and*
``STUDIUM_RUN_PAID_TESTS`` must be set. These tests spend real money on every
run, and a suite that quietly bills someone for running ``pytest`` is a bad
default no matter how small the amount.

**Why the runtime gets savepoint-joined sessions.** ``studium.asyncdb`` opens
its own session per call and commits (or rolls back) inside it -- that is the
whole point of the bridge, and it is correct in production. Under test it is a
problem: the seed data lives in an uncommitted transaction, so the runtime's
first ``read_db`` would roll it away and every later call would see an empty
database. Binding the runtime's sessions to the *same* connection with
``join_transaction_mode="create_savepoint"`` makes their commits and rollbacks
act on savepoints, so the outer transaction -- and the seed -- survives, and the
whole test still rolls back at the end. This is what lets a multi-call
end-to-end test run against real Postgres without committing anything.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text as sql
from sqlalchemy.orm import Session, sessionmaker

PAID_ENV = "STUDIUM_RUN_PAID_TESTS"


def _has_api_key() -> bool:
    return bool(
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )


@pytest.fixture(scope="session")
def paid_tests_enabled() -> bool:
    if not _has_api_key():
        pytest.skip("no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN in the environment")
    if os.environ.get(PAID_ENV, "").lower() not in {"1", "true", "yes"}:
        pytest.skip(
            f"{PAID_ENV} is not set; these tests make billable Anthropic calls"
        )
    return True


@pytest.fixture
def runtime_db(engine, monkeypatch) -> Iterator[Session]:
    """A session the runtime shares, on a transaction that is always rolled back.

    Replaces ``studium.asyncdb.SessionLocal`` for the duration of the test, so
    every ``run_db`` / ``read_db`` call lands on this connection.
    """
    connection = engine.connect()
    transaction = connection.begin()

    factory = sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    session = factory()

    # The runtime gets its own session per call, as it does in production --
    # just bound to this connection so nothing escapes the outer transaction.
    monkeypatch.setattr("studium.asyncdb.SessionLocal", factory)

    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture
def seeded(runtime_db: Session):
    """A lambda calculus fixture with an open session, inside a rolled-back tx.

    Reuses the subsystem 1 fixture rather than defining a second seed, so the
    runtime is exercised against the same data the data layer's own tests use.
    """
    from tests.fixtures import lambda_calculus

    fixture = lambda_calculus.build(
        runtime_db, email=f"runtime-{uuid.uuid4().hex[:8]}@example.com"
    )
    runtime_db.flush()

    session_id = runtime_db.execute(
        sql(
            """
            INSERT INTO learning_sessions
                (user_id, learner_subject_id, mode, focus_concept_id,
                 target_duration_minutes)
            VALUES (:user_id, :lsid, 'lecture', :concept_id, 90)
            RETURNING id
            """
        ),
        {
            "user_id": fixture.user.id,
            "lsid": fixture.enrollment.id,
            "concept_id": fixture.concept_id("beta-reduction"),
        },
    ).scalar_one()

    # Commit onto the savepoint so the runtime's own sessions can see the seed.
    runtime_db.commit()

    return {"fixture": fixture, "session_id": session_id, "db": runtime_db}
