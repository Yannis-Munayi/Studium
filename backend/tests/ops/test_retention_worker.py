"""The retention worker's algorithm (infrastructure §12, §16 Tier 1 line 4).

§16 Tier 1: "Retention worker algorithm handles edge cases (empty tables, large
batches, transaction failures)."

These are the offline half: the predicate composition, the hold-refusal rules,
the scheduler's clock arithmetic, and the failure isolation. The half that
needs rows in a database is in ``tests/online/test_ops_db.py``.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from studium.jobs import retention as policies
from studium.ops import nightly, retention, scheduler

# --- the deletion predicate ------------------------------------------------


def test_every_active_policy_composes_a_hold_exclusion() -> None:
    """A hold one path honours and another does not is worse than no hold.

    The data goes, and the row saying it was being kept is still there. Both
    ``apply_retention`` and the scheduled worker go through
    :func:`deletion_predicate` so they cannot disagree.
    """
    for policy in policies.active_policies():
        predicate = policies.deletion_predicate(policy)
        assert "retention_holds" in predicate, f"{policy.table} ignores holds"
        assert f"h.table_name = '{policy.table}'" in predicate
        assert "h.released_at IS NULL" in predicate, (
            f"{policy.table}'s exclusion would honour a released hold forever"
        )


def test_the_policy_predicate_is_parenthesised() -> None:
    """``a OR b AND NOT EXISTS(...)`` binds differently from ``(a OR b) AND ...``.

    Data layer §10's agent_traces policy is a two-clause predicate, so an
    unparenthesised compose would let AND take the second clause and delete
    rows the first clause excluded.
    """
    traces = next(p for p in policies.active_policies() if p.table == "agent_traces")
    predicate = policies.deletion_predicate(traces)
    assert predicate.startswith("("), predicate[:80]
    assert ") AND NOT EXISTS" in predicate.replace("\n", " ").replace("  ", " ") or (
        ")" in predicate.split("AND")[0]
    )


def test_active_policies_excludes_the_kept_forever_ones() -> None:
    """POLICIES documents both; only the windowed ones are executed.

    A "kept indefinitely" entry with no predicate would compose to
    ``WHERE None`` if it reached the worker.
    """
    active = {p.table for p in policies.active_policies()}
    all_tables = {p.table for p in policies.POLICIES}
    assert active < all_tables
    for policy in policies.active_policies():
        assert policy.window is not None and policy.predicate is not None


# --- holds -----------------------------------------------------------------


def test_holdable_tables_are_exactly_the_directly_deleted_ones() -> None:
    assert retention.holdable_tables() == {p.table for p in policies.active_policies()}


def test_hold_refuses_a_cascade_reached_table_and_names_the_parent() -> None:
    """The design decision, asserted rather than left in a docstring.

    A hold on ``session_turns`` would look exactly like protection and provide
    none: the ``learning_sessions`` policy deletes the parent and Postgres
    cascades with no predicate at all. The refusal has to name the parent, or
    the operator has no next step.
    """
    with pytest.raises(retention.HoldRefused) as caught:
        retention.place_hold(
            session=None,  # type: ignore[arg-type] -- refused before any query
            table="session_turns",
            row_id=uuid.uuid4(),
            reason="rights dispute",
        )
    message = str(caught.value)
    assert "cascade" in message
    assert "learning_sessions" in message, (
        "the refusal must name the parent to hold instead; without it the "
        "operator knows only that they cannot do the thing they came to do"
    )


def test_hold_refuses_a_table_with_no_retention_window() -> None:
    with pytest.raises(retention.HoldRefused, match="no retention window"):
        retention.place_hold(
            session=None,  # type: ignore[arg-type]
            table="journal_entries",
            row_id=uuid.uuid4(),
            reason="research study",
        )


def test_hold_refuses_an_empty_reason() -> None:
    """§12.4's examples are all reasons a human has to be able to re-read.

    A hold with no reason is indistinguishable from one placed by accident, and
    the whole point is that someone can later decide it has expired.
    """
    with pytest.raises(retention.HoldRefused, match="needs a reason"):
        retention.place_hold(
            session=None,  # type: ignore[arg-type]
            table="agent_traces",
            row_id=uuid.uuid4(),
            reason="   ",
        )


def test_cascade_parents_are_derived_from_the_schema() -> None:
    """Derived rather than listed, so it cannot drift from the foreign keys."""
    parents = retention.cascade_parents("session_turns")
    assert "learning_sessions" in parents
    # A table nothing cascades into from a policy-carrying parent.
    assert retention.cascade_parents("users") == ()
    # An unknown table degrades to empty rather than raising: the caller uses
    # this only to write a better message.
    assert retention.cascade_parents("no_such_table") == ()


# --- the scheduler's clock -------------------------------------------------


def test_next_run_is_the_coming_02_00_utc() -> None:
    now = dt.datetime(2026, 8, 26, 23, 30, tzinfo=dt.UTC)
    assert scheduler.seconds_until_next_run(now) == pytest.approx(2.5 * 3600)

    now = dt.datetime(2026, 8, 26, 1, 0, tzinfo=dt.UTC)
    assert scheduler.seconds_until_next_run(now) == pytest.approx(3600)


def test_exactly_on_the_hour_waits_a_full_day() -> None:
    """A deploy landing at 02:00:00.000 must not run twice in one night."""
    now = dt.datetime(2026, 8, 26, 2, 0, 0, tzinfo=dt.UTC)
    assert scheduler.seconds_until_next_run(now) == pytest.approx(24 * 3600)


def test_the_worker_is_off_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test constructing the app, or a developer running it, deletes nothing."""
    monkeypatch.delenv(scheduler.ENABLE_ENV, raising=False)
    assert not scheduler.enabled()
    monkeypatch.setenv(scheduler.ENABLE_ENV, "true")
    assert not scheduler.enabled(), "only the literal '1' arms it"
    monkeypatch.setenv(scheduler.ENABLE_ENV, "1")
    assert scheduler.enabled()


# --- failure isolation -----------------------------------------------------


class _ExplodingSession:
    """Fails every statement. Stands in for a lock timeout or a bad predicate."""

    def __init__(self) -> None:
        self.rollbacks = 0

    def execute(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("lock timeout")

    def rollback(self) -> None:
        self.rollbacks += 1

    def commit(self) -> None:
        pass


def test_a_failing_policy_does_not_stop_the_pass() -> None:
    """§16's "transaction failures".

    One bad predicate must not discard the other ten policies -- and must not
    take down the process the worker runs inside, which at 02:00 is the process
    serving anyone in a different timezone.
    """
    session = _ExplodingSession()
    run = retention.run_retention(session, record=False)  # type: ignore[arg-type]

    assert len(run.results) == len(policies.active_policies())
    assert len(run.failures) == len(run.results)
    for result in run.results:
        assert "lock timeout" in result.error
    assert run.rows_deleted == 0
    assert session.rollbacks == len(run.results), (
        "each failure must roll back its own transaction, or the next policy "
        "runs inside an aborted one and fails for the wrong reason"
    )


def test_a_failing_stage_does_not_stop_the_nightly_pass() -> None:
    session = _ExplodingSession()
    result = nightly.run_nightly(session)  # type: ignore[arg-type]

    assert [s.name for s in result.stages] == [
        "retention",
        "erasure_purge",
        "mastery_decay",
        "consistency",
    ]
    # Retention swallows its own policy failures, so it reports as a stage that
    # ran and found nothing to delete rather than as a failed stage. The three
    # after it fail, and the point is that all three still ran.
    assert len(result.failures) >= 3


def test_the_nightly_pass_runs_the_previously_orphaned_jobs() -> None:
    """The finding this module exists to pin.

    ``purge_expired_soft_deletes``, ``refresh_decay`` and
    ``check_dangling_chunk_refs`` were each written to run on a schedule and
    called by nothing but a test. A stage removed from this list is that state
    returning, and the symptom -- an erasure that never completes -- is not one
    any other test would notice.
    """
    import inspect

    source = inspect.getsource(nightly)
    for function in (
        "purge_expired_soft_deletes",
        "refresh_decay",
        "check_dangling_chunk_refs",
    ):
        assert function in source, (
            f"{function} has no caller again. It is scheduled work with no "
            f"scheduler, and nothing else in the suite would fail."
        )


def test_batch_ceiling_is_finite_and_reported() -> None:
    """A predicate matching everything empties the table politely, in batches.

    The ceiling turns that into a partial pass someone gets to look at, which
    is why hitting it is recorded rather than retried.
    """
    assert 0 < retention.MAX_BATCHES_PER_POLICY < 10_000
    assert 0 < retention.DEFAULT_BATCH_SIZE <= 10_000
    result = retention.PolicyResult(
        table="agent_traces", rows_deleted=1_000_000, duration_ms=1, truncated=True
    )
    assert "INCOMPLETE" in result.render()
