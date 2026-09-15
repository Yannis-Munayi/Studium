"""Operations against Postgres (infrastructure §16 Tier 2).

§16's Tier 2 list is five items. Three of them cannot run here and it is worth
saying which and why rather than quietly covering three of five:

* **Full deploy pipeline** and **rollback** need a Fly account and a scratch
  app. There is no deployment token in CI by design (§6.2 scopes it to a
  workstation), so those are run by hand against a scratch environment and
  recorded in ``docs/ops/drill-log.md``.
* **Backup restore drill** is quarterly and needs a real Fly backup (§9.3).
  What runs here is ``verify-restore`` -- the check the drill performs -- against
  a database whose contents we control, which is how you find out the check
  works before trusting what it says about a restore.
* **Secret rotation** is quarterly and touches provider consoles.

What does run here is the two that are genuinely testable against a database:
the retention worker against seeded rows, and the cost report against a seeded
ledger. Plus the migration-0012 behaviour that only a real Postgres enforces --
the partial unique index on live holds, the CHECK constraints, the
``updated_at`` trigger.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text as sql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from studium.ops import alerts, cost, nightly, restore, retention

pytestmark = pytest.mark.postgres


# --- helpers ---------------------------------------------------------------


def _user(db: Session, email: str = "ops@example.com") -> uuid.UUID:
    return db.execute(
        sql(
            """
            INSERT INTO users (email, display_name, role)
            VALUES (:email, 'Ops', 'learner')
            RETURNING id
            """
        ),
        {"email": email},
    ).scalar_one()


def _expired_auth_session(db: Session, user_id: uuid.UUID, *, days: int = 90) -> uuid.UUID:
    """A row the ``auth_sessions`` policy is due to delete.

    ``auth_sessions`` is chosen throughout because its predicate is the
    simplest of the eleven -- one column, no correlated subquery -- so a test
    that fails is failing on the worker rather than on the policy.
    """
    return db.execute(
        sql(
            """
            INSERT INTO auth_sessions (user_id, token_hash, expires_at)
            VALUES (:user_id, :token, NOW() - CAST(:days || ' days' AS interval))
            RETURNING id
            """
        ),
        {"user_id": user_id, "token": uuid.uuid4().hex * 2, "days": days},
    ).scalar_one()


# --- the retention worker --------------------------------------------------


def test_the_worker_deletes_what_is_past_its_window(db: Session) -> None:
    user_id = _user(db)
    doomed = _expired_auth_session(db, user_id, days=90)
    fresh = db.execute(
        sql(
            """
            INSERT INTO auth_sessions (user_id, token_hash, expires_at)
            VALUES (:user_id, :token, NOW() + INTERVAL '1 day')
            RETURNING id
            """
        ),
        {"user_id": user_id, "token": uuid.uuid4().hex * 2},
    ).scalar_one()

    run = retention.run_retention(db, record=False)

    assert not run.failures, [f.error for f in run.failures]
    assert _exists(db, "auth_sessions", fresh), "an unexpired session was deleted"
    assert not _exists(db, "auth_sessions", doomed)


def test_a_dry_run_counts_and_deletes_nothing(db: Session) -> None:
    user_id = _user(db)
    doomed = _expired_auth_session(db, user_id)

    run = retention.run_retention(db, dry_run=True, record=False)

    result = next(r for r in run.results if r.table == "auth_sessions")
    assert result.rows_deleted >= 1
    assert _exists(db, "auth_sessions", doomed), "a dry run deleted a row"


def test_an_empty_table_reports_zero_rather_than_being_skipped(db: Session) -> None:
    """§16's "empty tables", and the reason the zero matters.

    §12.2's audit trail answers "why did that data go away". It only answers
    the other case -- "nothing deleted it, investigate" -- if a pass that
    deleted nothing is distinguishable from a pass that never ran.
    """
    run = retention.run_retention(db, record=False)
    tables = {r.table for r in run.results}
    from studium.jobs.retention import active_policies

    assert tables == {p.table for p in active_policies()}
    for result in run.results:
        assert result.ok
        assert result.rows_deleted >= 0


def test_a_hold_protects_a_row_from_the_worker(db: Session) -> None:
    """§12.4, end to end: place, run, and find the row still there."""
    user_id = _user(db)
    doomed = _expired_auth_session(db, user_id)
    also_doomed = _expired_auth_session(db, user_id)

    hold_id = retention.place_hold(
        db,
        table="auth_sessions",
        row_id=doomed,
        reason="evidence in complaint 2026-114",
        placed_by=user_id,
    )
    db.flush()

    run = retention.run_retention(db, record=False)

    assert _exists(db, "auth_sessions", doomed), "the held row was deleted"
    assert not _exists(db, "auth_sessions", also_doomed)
    result = next(r for r in run.results if r.table == "auth_sessions")
    assert result.held_rows == 1

    # And releasing it lets the next pass through.
    assert retention.release_hold(db, hold_id, reason="complaint closed")
    db.flush()
    retention.run_retention(db, record=False)
    assert not _exists(db, "auth_sessions", doomed)


def test_a_released_hold_is_kept_as_a_record(db: Session) -> None:
    """A hold that vanished when lifted would destroy the only record that the
    data was deliberately kept -- which is what the dispute would later ask
    about."""
    user_id = _user(db)
    row_id = _expired_auth_session(db, user_id)
    hold_id = retention.place_hold(
        db, table="auth_sessions", row_id=row_id, reason="dispute"
    )
    retention.release_hold(db, hold_id, reason="resolved")
    db.flush()

    live = retention.list_holds(db)
    everything = retention.list_holds(db, include_released=True)
    assert not [h for h in live if h["id"] == hold_id]
    kept = next(h for h in everything if h["id"] == hold_id)
    assert kept["released_reason"] == "resolved"
    assert kept["reason"] == "dispute"


def test_a_second_hold_on_the_same_row_updates_rather_than_stacks(db: Session) -> None:
    """Two live holds with different reasons make "why is this still here"
    unanswerable. The partial unique index is what enforces it."""
    user_id = _user(db)
    row_id = _expired_auth_session(db, user_id)

    first = retention.place_hold(
        db, table="auth_sessions", row_id=row_id, reason="first reason"
    )
    second = retention.place_hold(
        db, table="auth_sessions", row_id=row_id, reason="second reason"
    )
    db.flush()

    assert first == second
    holds = [h for h in retention.list_holds(db) if h["row_id"] == row_id]
    assert len(holds) == 1
    assert holds[0]["reason"] == "second reason"


def test_a_hold_on_a_missing_row_is_refused(db: Session) -> None:
    """The usual cause is a typo'd id, and a hold on a nonexistent row is
    indistinguishable from one that worked until someone goes looking."""
    with pytest.raises(retention.HoldRefused, match="no row"):
        retention.place_hold(
            db, table="auth_sessions", row_id=uuid.uuid4(), reason="typo"
        )


def test_the_pass_writes_one_audit_row_per_policy(db: Session) -> None:
    """§12.2. Including the zeros -- see the empty-table test above."""
    from studium.jobs.retention import active_policies

    before = db.execute(sql("SELECT count(*) FROM retention_actions")).scalar_one()
    run = retention.run_retention(db)
    db.flush()
    after = db.execute(sql("SELECT count(*) FROM retention_actions")).scalar_one()

    assert after - before == len(active_policies())

    rows = db.execute(
        sql(
            """
            SELECT table_name, rows_deleted, duration_ms, metadata
              FROM retention_actions ORDER BY ran_at DESC
             LIMIT :n
            """
        ),
        {"n": len(active_policies())},
    ).all()
    run_ids = {row.metadata["run_id"] for row in rows}
    assert run_ids == {str(run.run_id)}, "one pass must share one run_id"
    for row in rows:
        assert row.duration_ms >= 0
        assert "policy_window_days" in row.metadata


def test_retention_actions_refuses_a_negative_count(db: Session) -> None:
    """The value of this table is that its numbers can be trusted without
    re-deriving them from what they describe."""
    with pytest.raises(IntegrityError):
        db.execute(
            sql(
                """
                INSERT INTO retention_actions (table_name, rows_deleted, duration_ms)
                VALUES ('agent_traces', -1, 5)
                """
            )
        )


def test_a_hold_cannot_be_released_before_it_was_placed(db: Session) -> None:
    user_id = _user(db)
    row_id = _expired_auth_session(db, user_id)
    retention.place_hold(db, table="auth_sessions", row_id=row_id, reason="x")
    db.flush()
    with pytest.raises(IntegrityError):
        db.execute(
            sql(
                "UPDATE retention_holds SET released_at = placed_at - INTERVAL '1 day' "
                " WHERE row_id = :row_id"
            ),
            {"row_id": row_id},
        )


def test_the_holds_table_has_a_working_updated_at_trigger(db: Session) -> None:
    """Migration 0012 installs ``trg_retention_holds_updated_at``.

    Two facts make the obvious version of this test wrong, and both are worth
    stating because they make it look like a broken trigger:

    * ``NOW()`` is Postgres's *transaction* clock and does not advance inside
      one, so comparing ``updated_at`` before and after an UPDATE compares a
      value to itself. The ``db`` fixture is a single transaction.
    * The trigger is ``BEFORE UPDATE``, so it cannot be defeated by back-dating
      the column with an UPDATE -- that statement fires it too.

    So the row is *inserted* with an old ``updated_at`` (which the trigger does
    not see) and then updated.
    """
    user_id = _user(db)
    row_id = _expired_auth_session(db, user_id)
    hold_id = db.execute(
        sql(
            """
            INSERT INTO retention_holds
                (table_name, row_id, reason, updated_at)
            VALUES ('auth_sessions', :row_id, 'x', NOW() - INTERVAL '1 year')
            RETURNING id
            """
        ),
        {"row_id": row_id},
    ).scalar_one()
    stale = db.execute(
        sql("SELECT updated_at FROM retention_holds WHERE id = :id"), {"id": hold_id}
    ).scalar_one()

    db.execute(
        sql("UPDATE retention_holds SET reason = 'y' WHERE id = :id"), {"id": hold_id}
    )
    after = db.execute(
        sql("SELECT updated_at FROM retention_holds WHERE id = :id"), {"id": hold_id}
    ).scalar_one()

    assert after > stale, "no updated_at trigger on retention_holds"


# --- the nightly pass ------------------------------------------------------


def test_the_nightly_pass_hard_deletes_an_expired_soft_delete(db: Session) -> None:
    """Data layer §10 step 4, which had no scheduled caller before this build.

    An erasure request was therefore never completing: the account is marked
    deleted, the 30 days expire, and the row stays forever because nothing came
    back for it.
    """
    from studium.privacy import DISPUTE_WINDOW_DAYS

    user_id = _user(db, "erased@example.com")
    db.execute(
        sql(
            f"""
            UPDATE users
               SET deleted_at = NOW() - INTERVAL '{DISPUTE_WINDOW_DAYS + 1} days'
             WHERE id = :id
            """
        ),
        {"id": user_id},
    )
    recent = _user(db, "recent@example.com")
    db.execute(
        sql("UPDATE users SET deleted_at = NOW() WHERE id = :id"), {"id": recent}
    )
    db.flush()

    result = nightly.run_nightly(db)

    assert not result.failures, [(f.name, f.error) for f in result.failures]
    assert not _exists(db, "users", user_id), "the expired soft delete survived"
    assert _exists(db, "users", recent), "an account inside the window was deleted"

    stage = next(s for s in result.stages if s.name == "erasure_purge")
    assert "1 account" in stage.detail


def test_the_nightly_mastery_stage_refreshes_a_stale_decayed_value(
    db: Session,
) -> None:
    """``refresh_decay``'s effect, not just that it runs without raising.

    Every other nightly test runs this stage against an empty
    ``concept_mastery``, where a no-op and a correct UPDATE are the same
    observation: zero rows, no error. The orphaning symptom was a *stale
    column*, so the assertion has to be that a stale value moves.

    Gating is deliberately unaffected -- ``graph.unlock_status`` computes decay
    in SQL -- which is why nobody noticed the column was frozen. What reads it
    is sorting and reporting.
    """
    subject_id, concept_id = _seed_curriculum(db)
    user_id = _user(db, f"decay-{uuid.uuid4().hex[:8]}@example.com")
    enrollment_id = db.execute(
        sql(
            """
            INSERT INTO learner_subjects (user_id, subject_id, subject_version)
            VALUES (:uid, :sid, 1) RETURNING id
            """
        ),
        {"uid": user_id, "sid": subject_id},
    ).scalar_one()

    # p_known 0.9, last touched a year ago, and a decayed column still holding
    # the undecayed value -- exactly the state a year with no scheduler leaves.
    mastery_id = db.execute(
        sql(
            """
            INSERT INTO concept_mastery (learner_subject_id, concept_id, p_known,
                                         p_known_decayed, last_evidence_at)
            VALUES (:lsid, :cid, 0.9, 0.9, NOW() - INTERVAL '365 days')
            RETURNING id
            """
        ),
        {"lsid": enrollment_id, "cid": concept_id},
    ).scalar_one()
    db.flush()

    result = nightly.run_nightly(db)

    stage = next(s for s in result.stages if s.name == "mastery_decay")
    assert stage.ok, stage.error

    refreshed = db.execute(
        sql("SELECT p_known, p_known_decayed FROM concept_mastery WHERE id = :id"),
        {"id": mastery_id},
    ).one()
    assert refreshed.p_known == pytest.approx(0.9), "the raw posterior is not decay's to touch"
    assert refreshed.p_known_decayed < 0.9, (
        "a year past the last evidence and the decayed value never moved -- "
        "the stage ran but the column is still what the learner's last write left"
    )
    assert 0.0 <= refreshed.p_known_decayed <= 1.0


def test_the_nightly_pass_runs_every_stage(db: Session) -> None:
    result = nightly.run_nightly(db)
    assert [s.name for s in result.stages] == [
        "retention",
        "erasure_purge",
        "mastery_decay",
        "consistency",
    ]
    assert not result.failures, [(f.name, f.error) for f in result.failures]


def test_a_dry_run_of_the_nightly_pass_changes_nothing(db: Session) -> None:
    from studium.privacy import DISPUTE_WINDOW_DAYS

    user_id = _user(db, "dryrun@example.com")
    db.execute(
        sql(
            f"UPDATE users SET deleted_at = NOW() - INTERVAL "
            f"'{DISPUTE_WINDOW_DAYS + 1} days' WHERE id = :id"
        ),
        {"id": user_id},
    )
    db.flush()

    result = nightly.run_nightly(db, dry_run=True)

    assert _exists(db, "users", user_id)
    assert "dry run" in next(s for s in result.stages if s.name == "erasure_purge").detail


# --- verify-restore --------------------------------------------------------


def test_verify_restore_passes_against_a_healthy_database(db: Session) -> None:
    """The check §9.3's drill runs. Testing it here is how you find out the
    check works before trusting what it says about a restore.

    The subject and concept are seeded because a CI database is fresh, and
    ``MUST_NOT_BE_EMPTY`` asserts what a *working deployment* looks like. That
    distinction is the check's whole value -- "empty because new" and "empty
    because the restore dropped it" are the two states it exists to tell
    apart -- so seeding here rather than shortening the list is the version
    that keeps the check meaning something.
    """
    _seed_curriculum(db)
    verification = restore.verify(db)
    assert verification.ok, "\n".join(c.render() for c in verification.failures)

    names = {c.name for c in verification.checks}
    assert {"extensions", "schema version", "tables present"} <= names


def test_verify_restore_reports_an_empty_curriculum_as_a_failure(db: Session) -> None:
    """The other half of the distinction above.

    A restored database with no concepts is one the backend comes up against
    and serves nothing from, and the restore reported success.
    """
    empty = db.execute(sql("SELECT count(*) FROM concepts")).scalar_one() == 0
    if not empty:
        pytest.skip("this database already has a curriculum")
    verification = restore.verify(db)
    assert not verification.ok
    assert any("concepts" in c.name for c in verification.failures)


def test_verify_restore_notices_a_schema_at_the_wrong_revision(db: Session) -> None:
    """A restore from before a migration is a database the current code fails
    against on its first query -- and at 03:00 that reads as a code bug."""
    verification = restore.verify(db, expected_head="9999")
    assert not verification.ok
    assert any(c.name == "schema version" for c in verification.failures)


def test_verify_restore_finds_a_dangling_chunk_reference(db: Session) -> None:
    """The one integrity property Postgres will not re-check on restore.

    ``concept_sources.chunk_ids`` is an array and cannot carry a foreign key
    (data layer §6.2), so a partial restore breaks it silently.
    """
    _dangling_chunk_reference(db)

    verification = restore.verify(db)
    check = next(c for c in verification.checks if "chunk_ids" in c.name)
    assert not check.passed
    assert not verification.ok


def test_the_nightly_consistency_stage_queues_a_dangling_reference(
    db: Session,
) -> None:
    """``check_dangling_chunk_refs`` had no caller anywhere before this build.

    It is flagged into ``ingestion_review_queue`` rather than logged: that
    table is the surface SPEC_DEBT SD1 asked for, and logging is precisely the
    stopgap SD1 was written to complain about.
    """
    _dangling_chunk_reference(db)

    result = nightly.run_nightly(db)
    stage = next(s for s in result.stages if s.name == "consistency")
    assert stage.ok, stage.error
    assert "1 newly queued" in stage.detail

    queued = db.execute(
        sql(
            """
            SELECT count(*) FROM ingestion_review_queue
             WHERE flag_source = 'concept_source_conflict'
               AND payload ->> 'found_by' LIKE 'nightly%'
            """
        )
    ).scalar_one()
    assert queued == 1

    # And a second pass does not queue it again. This runs every night; a
    # dangling reference nobody has fixed would otherwise produce 365
    # identical queue items a year, which is a queue nobody opens.
    again = nightly.run_nightly(db)
    stage = next(s for s in again.stages if s.name == "consistency")
    assert "0 newly queued" in stage.detail


def test_head_revision_reads_the_migration_files() -> None:
    """From the files rather than from Alembic's config, so the check works in
    a container with no alembic.ini -- which is the container an operator is in
    during a restore."""
    assert restore.head_revision() is not None
    assert restore.head_revision().isdigit()


# --- alerts ----------------------------------------------------------------


def test_every_probe_runs_against_a_real_schema(db: Session) -> None:
    """The probes are SQL, and SQL that has never touched the schema it queries
    is SQL that compiles in a docstring."""
    results = alerts.evaluate(db)
    probed = [c for c in alerts.CONDITIONS if c.probe is not None]
    assert len(results) == len(probed)
    for alert in results:
        assert isinstance(alert.measurement.value, int | float)
        assert alert.measurement.detail


def test_the_queue_depth_alert_fires_over_its_threshold(db: Session) -> None:
    limit = int(alerts.THRESHOLDS["ingestion_queue_depth"])
    subject_id = db.execute(
        sql(
            """
            INSERT INTO subjects (slug, title, short_description, long_description)
            VALUES ('queue-probe', 'T', 's', 'l') RETURNING id
            """
        )
    ).scalar_one()
    for index in range(limit + 1):
        db.execute(
            sql(
                """
                INSERT INTO ingestion_review_queue
                    (subject_id, flag_source, reason, severity)
                VALUES (:sid, 'license_pending', :reason, 2)
                """
            ),
            {"sid": subject_id, "reason": f"probe {index}"},
        )
    db.flush()

    [alert] = alerts.evaluate(db, keys=["ingestion_review_queue_depth"])
    assert alert.measurement.firing
    assert alert.measurement.value >= limit + 1


def test_the_stale_worker_alert_distinguishes_never_run_from_stopped(
    db: Session,
) -> None:
    """A worker that has never run is a fresh database, not a fault.

    Firing on it would make the alert red on every new deployment until the
    first 02:00, which is how an alert stops being read.
    """
    [alert] = alerts.evaluate(db, keys=["retention_worker_stale"])
    assert not alert.measurement.firing
    assert "never run" in alert.measurement.detail

    db.execute(
        sql(
            """
            INSERT INTO retention_actions
                (ran_at, table_name, rows_deleted, duration_ms)
            VALUES (NOW() - INTERVAL '3 days', 'agent_traces', 0, 1)
            """
        )
    )
    db.flush()
    [alert] = alerts.evaluate(db, keys=["retention_worker_stale"])
    assert alert.measurement.firing


# --- the cost report -------------------------------------------------------


def test_the_cost_report_sums_by_line_and_by_user(db: Session) -> None:
    user_id = _user(db, "spender@example.com")
    today = dt.date.today()
    db.execute(
        sql(
            """
            INSERT INTO cost_ledger
                (user_id, day, model, cost_agent_usd, cost_content_usd)
            VALUES (:uid, :day, 'claude-opus-4-8', 1.50, 0.25)
            """
        ),
        {"uid": user_id, "day": today},
    )
    db.flush()

    report = cost.report(db, start=today, end=today)

    assert report.by_line["agent"] == pytest.approx(1.50)
    assert report.by_line["content"] == pytest.approx(0.25)
    assert report.total_usd == pytest.approx(1.75)
    spender = next(u for u in report.by_user if u.email == "spender@example.com")
    assert spender.total_usd == pytest.approx(1.75)
    assert "spender@example.com" in report.render()


def test_the_report_says_when_the_window_includes_today(db: Session) -> None:
    """The ledger is a day behind by design; a report that silently added an
    inconsistent figure for today would be worse than one that says so."""
    today = dt.date.today()
    assert cost.report(db, start=today, end=today).includes_today
    yesterday = today - dt.timedelta(days=1)
    assert not cost.report(db, start=yesterday, end=yesterday).includes_today


def test_the_anomaly_rule_needs_trailing_history(db: Session) -> None:
    """§13.1: "> 3x their trailing 7-day average".

    A learner's first day has no trailing average and is not an anomaly.
    Treating it as an infinite multiple would flag every new learner.
    """
    user_id = _user(db, "newcomer@example.com")
    today = dt.date.today()
    db.execute(
        sql(
            """
            INSERT INTO cost_ledger (user_id, day, model, cost_agent_usd)
            VALUES (:uid, :day, 'claude-opus-4-8', 50.00)
            """
        ),
        {"uid": user_id, "day": today},
    )
    db.flush()
    assert not cost.anomalies(db, start=today, end=today)


def test_the_anomaly_rule_fires_on_a_real_spike(db: Session) -> None:
    user_id = _user(db, "spike@example.com")
    today = dt.date.today()
    for offset in range(1, 8):
        db.execute(
            sql(
                """
                INSERT INTO cost_ledger (user_id, day, model, cost_agent_usd)
                VALUES (:uid, :day, 'claude-opus-4-8', 1.00)
                """
            ),
            {"uid": user_id, "day": today - dt.timedelta(days=offset)},
        )
    db.execute(
        sql(
            """
            INSERT INTO cost_ledger (user_id, day, model, cost_agent_usd)
            VALUES (:uid, :day, 'claude-opus-4-8', 10.00)
            """
        ),
        {"uid": user_id, "day": today},
    )
    db.flush()

    found = cost.anomalies(db, start=today, end=today)
    spike = next(a for a in found if a.email == "spike@example.com")
    assert spike.multiple == pytest.approx(10.0)


def test_the_report_surfaces_unattributed_content(db: Session) -> None:
    """SPEC_DEBT SD2's closure. The figure had one reader -- an integrity test --
    and no operational surface at all."""
    report = cost.report(
        db, start=dt.date.today() - dt.timedelta(days=1), end=dt.date.today()
    )
    assert isinstance(report.unattributed_content, tuple)
    assert "attribution health" in report.render()


# --- signing key compromise (§11.4) ----------------------------------------


def test_marking_a_key_compromised_writes_an_audit_row(db: Session) -> None:
    from studium.ops import keys as key_ops

    _publish_key(db, "probe-key", "A" * 44)

    # The database's clock, not Python's: ``activated_at`` defaults to
    # Postgres's NOW() and the CHECK compares against it, so a container clock
    # that differs from the host's would fail this on an offset rather than on
    # the behaviour.
    earliest = _db_now(db) - dt.timedelta(days=2)
    assert key_ops.mark_compromised(
        db, "probe-key", earliest_possible=earliest, reason="seed in a scrollback"
    )
    db.flush()

    stamped = db.execute(
        sql("SELECT compromised_at FROM signing_keys WHERE key_id = 'probe-key'")
    ).scalar_one()
    assert stamped is not None
    logged = db.execute(
        sql(
            "SELECT count(*) FROM audit_log WHERE action = 'signing_key_compromised'"
        )
    ).scalar_one()
    assert logged == 1


def test_a_compromise_mark_can_be_widened_and_not_narrowed(db: Session) -> None:
    """Narrowing a published scrutiny window retroactively tells verifiers that
    credentials they were warned about are fine."""
    from studium.ops import keys as key_ops

    _publish_key(db, "probe-key-2", "B" * 44)

    now = _db_now(db)
    key_ops.mark_compromised(
        db, "probe-key-2", earliest_possible=now - dt.timedelta(hours=2), reason="r"
    )
    db.flush()

    # Later estimate: refused, window unchanged.
    assert not key_ops.mark_compromised(
        db, "probe-key-2", earliest_possible=now - dt.timedelta(hours=1), reason="r"
    )
    # Earlier estimate: accepted, window widens.
    assert key_ops.mark_compromised(
        db, "probe-key-2", earliest_possible=now - dt.timedelta(hours=5), reason="r"
    )
    db.flush()
    widened = db.execute(
        sql("SELECT compromised_at FROM signing_keys WHERE key_id = 'probe-key-2'")
    ).scalar_one()
    assert widened == now - dt.timedelta(hours=5)


def test_a_compromise_before_activation_is_refused(db: Session) -> None:
    from studium.ops import keys as key_ops

    _publish_key(db, "probe-key-3", "C" * 44, activated_days_ago=0)
    with pytest.raises(ValueError, match="before the key was activated"):
        key_ops.mark_compromised(
            db,
            "probe-key-3",
            earliest_possible=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
            reason="r",
        )


# --- helper ----------------------------------------------------------------


def _publish_key(
    db: Session, key_id: str, public_key: str, *, activated_days_ago: int = 30
) -> None:
    """A published key, back-dated so a compromise window has room.

    ``activated_at`` defaults to NOW() and a CHECK forbids ``compromised_at``
    before it, so a key activated this instant cannot carry any window at all.
    Every real key has been in service for a while by the time anyone suspects
    it, which is what the back-dating represents.

    ``retired_at`` is set so the partial unique index (one un-retired key per
    issuer) does not reject the second and third probe keys.
    """
    db.execute(
        sql(
            """
            INSERT INTO signing_keys
                (key_id, algorithm, public_key, issuer, activated_at, retired_at)
            VALUES (:key_id, 'ed25519', :pk, :issuer,
                    NOW() - CAST(:days || ' days' AS interval), NOW())
            """
        ),
        {
            "key_id": key_id,
            "pk": public_key,
            "issuer": f"probe-{key_id}",
            "days": activated_days_ago,
        },
    )
    db.flush()


def _db_now(db: Session) -> dt.datetime:
    """Postgres's transaction clock.

    Several timestamps here default to ``NOW()`` and are compared against by a
    CHECK, so the database's own clock is the only one whose relationship to
    them is guaranteed.
    """
    return db.execute(sql("SELECT NOW()")).scalar_one()


def _exists(db: Session, table: str, row_id: uuid.UUID) -> bool:
    return bool(
        db.execute(
            sql(f"SELECT 1 FROM {table} WHERE id = :id"), {"id": row_id}
        ).scalar()
    )


def _seed_curriculum(db: Session) -> tuple[uuid.UUID, uuid.UUID]:
    """The minimum a working deployment has: one subject, one concept."""
    slug = f"ops-probe-{uuid.uuid4().hex[:8]}"
    subject_id = db.execute(
        sql(
            """
            INSERT INTO subjects (slug, title, short_description, long_description)
            VALUES (:slug, 'Ops probe', 'short', 'long') RETURNING id
            """
        ),
        {"slug": slug},
    ).scalar_one()
    concept_id = db.execute(
        sql(
            """
            INSERT INTO concepts (subject_id, slug, title)
            VALUES (:sid, :slug, 'Concept') RETURNING id
            """
        ),
        {"sid": subject_id, "slug": f"{slug}-concept"},
    ).scalar_one()
    db.flush()
    return subject_id, concept_id


def _dangling_chunk_reference(db: Session) -> uuid.UUID:
    """A ``concept_sources`` row pointing at a chunk that does not exist."""
    subject_id, concept_id = _seed_curriculum(db)
    source_id = db.execute(
        sql(
            """
            INSERT INTO sources (subject_id, title, license, storage_path,
                                 content_sha256)
            VALUES (:sid, 'Probe source', 'public_domain', '', :hash)
            RETURNING id
            """
        ),
        {"sid": subject_id, "hash": uuid.uuid4().hex * 2},
    ).scalar_one()
    row_id = db.execute(
        sql(
            """
            INSERT INTO concept_sources (concept_id, source_id, role, chunk_ids)
            VALUES (:cid, :srcid, 'primary_exposition', ARRAY[:missing]::uuid[])
            RETURNING id
            """
        ),
        {"cid": concept_id, "srcid": source_id, "missing": uuid.uuid4()},
    ).scalar_one()
    db.flush()
    return row_id
