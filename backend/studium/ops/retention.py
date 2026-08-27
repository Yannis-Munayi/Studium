"""The scheduled retention worker and its holds (infrastructure §12).

Data layer §10 defines *what* is kept and for how long; ``studium.jobs.retention``
holds those policies. This module is the operational half §12 asks for: a
nightly pass that respects holds, records what it did, and cannot take the
product down when a policy fails.

Three properties, each load-bearing:

**A transaction per policy, not per pass.** A single transaction across eleven
policies means one lock-timeout on ``agent_traces`` discards ten successful
deletes *and* the audit rows describing them, so the next morning's evidence of
what happened is the absence of evidence. Committing per policy costs nothing
-- the policies are independent by construction -- and turns a partial pass
into a partial pass that says which half it was.

**Batched deletes.** ``DELETE FROM agent_traces WHERE created_at < ...`` on a
year's accumulation is one statement holding row locks for as long as it takes.
The learner's turn writing a trace at that moment waits behind it. Deleting in
bounded batches with a commit between them keeps the longest lock proportional
to the batch rather than to the backlog, which is the difference between a
nightly job and an outage at 02:00.

**A row per policy per pass, including zeros.** §12.2's audit trail answers
"why did that data go away". It only answers the *other* case -- "nothing
deleted it, investigate" -- if a pass that deleted nothing is distinguishable
from a pass that never ran. ``rows_deleted = 0`` is a recorded observation.
``alerts.retention_worker_stale`` reads exactly this.

**Holds are only honoured where the worker deletes directly.** See
:func:`place_hold`.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.jobs.retention import Policy, active_policies, deletion_predicate

log = logging.getLogger(__name__)

#: Rows deleted per statement. Sized so the longest lock a learner's turn can
#: queue behind is milliseconds at MVP volume, and so a pass that is genuinely
#: enormous (the first one after a year of not running) still makes progress
#: rather than timing out and losing all of it.
DEFAULT_BATCH_SIZE = 2_000

#: Ceiling on batches per policy per pass. A policy whose predicate has been
#: mis-written to match everything would otherwise empty the table in one
#: night, in batches, politely. Hitting the ceiling is recorded in the audit
#: row's metadata and reported by the CLI as an incomplete policy -- the next
#: pass continues, and someone gets to look first.
MAX_BATCHES_PER_POLICY = 500


@dataclass(slots=True)
class PolicyResult:
    """What one policy did in one pass."""

    table: str
    rows_deleted: int
    duration_ms: int
    batches: int = 0
    truncated: bool = False
    error: str = ""
    held_rows: int = 0

    @property
    def ok(self) -> bool:
        return not self.error

    def render(self) -> str:
        if self.error:
            return f"  {self.table:26} FAILED  {self.error}"
        note = ""
        if self.truncated:
            note = f"  (stopped at {MAX_BATCHES_PER_POLICY} batches -- INCOMPLETE)"
        elif self.held_rows:
            note = f"  ({self.held_rows} row(s) under hold, skipped)"
        return (
            f"  {self.table:26} {self.rows_deleted:>8} rows  "
            f"{self.duration_ms:>6} ms{note}"
        )


@dataclass(slots=True)
class RetentionRun:
    """One nightly pass."""

    run_id: uuid.UUID
    started_at: dt.datetime
    dry_run: bool
    results: list[PolicyResult] = field(default_factory=list)

    @property
    def rows_deleted(self) -> int:
        return sum(r.rows_deleted for r in self.results)

    @property
    def failures(self) -> list[PolicyResult]:
        return [r for r in self.results if not r.ok]

    def render(self) -> str:
        mode = "DRY RUN -- nothing deleted" if self.dry_run else "applied"
        lines = [
            f"retention pass {self.run_id} ({mode})",
            f"  started {self.started_at.isoformat()}",
            "",
        ]
        lines += [r.render() for r in self.results]
        lines.append("")
        lines.append(
            f"  {len(self.results)} policies, {self.rows_deleted} rows, "
            f"{len(self.failures)} failure(s)"
        )
        return "\n".join(lines)


def _held_count(session: Session, table: str) -> int:
    return int(
        session.execute(
            sql(
                "SELECT count(*) FROM retention_holds "
                " WHERE table_name = :t AND released_at IS NULL"
            ),
            {"t": table},
        ).scalar_one()
    )


def _delete_batches(
    session: Session, policy: Policy, *, batch_size: int
) -> tuple[int, int, bool]:
    """Delete in bounded batches. Returns (rows, batches, truncated).

    ``DELETE ... WHERE id IN (SELECT id ... LIMIT n)`` rather than a plain
    ``LIMIT`` on the delete, which Postgres does not accept. The subquery takes
    ``FOR UPDATE SKIP LOCKED`` so a batch never waits on a row some other
    transaction is holding -- at 02:00 that is a live session's own write, and
    blocking it to delete a year-old trace is the wrong priority. A skipped row
    is picked up by the next pass, which is what a retention window measured in
    days can afford.
    """
    predicate = deletion_predicate(policy)
    statement = sql(
        f"""
        DELETE FROM {policy.table}
         WHERE id IN (
             SELECT id FROM {policy.table}
              WHERE {predicate}
              LIMIT :batch
              FOR UPDATE SKIP LOCKED
         )
        """
    )

    total = 0
    for batch in range(MAX_BATCHES_PER_POLICY):
        deleted = int(session.execute(statement, {"batch": batch_size}).rowcount or 0)
        session.commit()
        total += deleted
        if deleted < batch_size:
            return total, batch + 1, False
    return total, MAX_BATCHES_PER_POLICY, True


def _count_eligible(session: Session, policy: Policy) -> int:
    return int(
        session.execute(
            sql(f"SELECT count(*) FROM {policy.table} WHERE {deletion_predicate(policy)}")
        ).scalar_one()
    )


def run_retention(
    session: Session,
    *,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    record: bool = True,
) -> RetentionRun:
    """Execute one pass of every active policy (§12.1).

    ``record=False`` suppresses the ``retention_actions`` writes. It exists for
    the dry run and for the Tier 2 tests, and is deliberately not the default:
    a pass that deleted rows and wrote no audit row is the exact state §12.2
    exists to make impossible.

    Never raises for a policy failure. One bad predicate must not stop the
    other ten from running, and it must not take down the process the worker
    runs inside -- the failure lands in the audit row's metadata and in
    :attr:`RetentionRun.failures`, where the CLI's exit code reads it.
    """
    run = RetentionRun(
        run_id=uuid.uuid4(), started_at=dt.datetime.now(dt.UTC), dry_run=dry_run
    )

    for policy in active_policies():
        started = time.perf_counter()
        held = 0
        try:
            # Inside the try, not before it. The hold count is a query like any
            # other, and a database that has stopped answering fails it exactly
            # as it fails the delete -- outside the guard that failure escaped
            # the per-policy isolation and took the whole pass down, which is
            # the opposite of what this loop is for. Found by
            # tests/ops/test_retention_worker.py, which drives a session that
            # raises on every statement.
            held = _held_count(session, policy.table)
            if dry_run:
                rows, batches, truncated = _count_eligible(session, policy), 0, False
            else:
                rows, batches, truncated = _delete_batches(
                    session, policy, batch_size=batch_size
                )
        except Exception as exc:  # noqa: BLE001 -- recorded, not raised
            session.rollback()
            log.exception("retention policy for %s failed", policy.table)
            result = PolicyResult(
                table=policy.table,
                rows_deleted=0,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
                held_rows=held,
            )
        else:
            result = PolicyResult(
                table=policy.table,
                rows_deleted=rows,
                duration_ms=int((time.perf_counter() - started) * 1000),
                batches=batches,
                truncated=truncated,
                held_rows=held,
            )

        run.results.append(result)
        if record and not dry_run:
            _record(session, run, policy, result)

    return run


def _record(
    session: Session, run: RetentionRun, policy: Policy, result: PolicyResult
) -> None:
    """Write the §12.2 audit row. Committed on its own.

    Failing to write the audit row is logged and swallowed: losing one line of
    the trail is bad, and losing the ten deletes that already committed plus
    the nine policies still to run because the trail could not be written is
    worse. The stale-worker alert catches a trail that stops entirely.
    """
    metadata = {
        "run_id": str(run.run_id),
        "policy_window_days": policy.window.days if policy.window else None,
        "policy_note": policy.note,
        "batches": result.batches,
        "batch_truncated": result.truncated,
        "held_rows": result.held_rows,
        "dry_run": run.dry_run,
    }
    if result.error:
        metadata["error"] = result.error
    try:
        session.execute(
            sql(
                """
                INSERT INTO retention_actions
                    (table_name, rows_deleted, duration_ms, metadata)
                VALUES
                    (:table, :rows, :duration, CAST(:metadata AS jsonb))
                """
            ),
            {
                "table": result.table,
                "rows": result.rows_deleted,
                "duration": result.duration_ms,
                "metadata": json.dumps(metadata),
            },
        )
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        log.exception("could not record retention action for %s", result.table)


# --- holds (§12.4) ---------------------------------------------------------


class HoldRefused(ValueError):
    """A hold was requested on a table where it would not protect anything."""


def holdable_tables() -> frozenset[str]:
    """Tables the worker deletes from directly, and therefore can protect."""
    return frozenset(p.table for p in active_policies())


def cascade_parents(table: str) -> tuple[str, ...]:
    """Policy-carrying tables whose deletion cascades into ``table``.

    Derived from the ORM metadata rather than listed by hand: the set changes
    whenever a foreign key does, and a hard-coded list is one that silently
    stops being true. Used only to write a better refusal message, so a miss
    degrades to a vaguer sentence rather than to a wrong one.
    """
    from studium.models import Base

    target = Base.metadata.tables.get(table)
    if target is None:
        return ()
    holdable = holdable_tables()
    parents = {
        fk.column.table.name
        for fk in target.foreign_keys
        if fk.ondelete and fk.ondelete.upper() == "CASCADE"
        and fk.column.table.name in holdable
    }
    return tuple(sorted(parents))


def place_hold(
    session: Session,
    *,
    table: str,
    row_id: uuid.UUID,
    reason: str,
    placed_by: uuid.UUID | None = None,
) -> uuid.UUID:
    """Protect one row from the retention worker (§12.4).

    **Refuses tables the worker does not delete from directly**, and this is
    the design decision worth stating rather than the mechanism.

    A hold is a row in a table. The worker consults it in the ``WHERE`` clause
    of its own ``DELETE``. Postgres's referential actions consult nothing: when
    the ``learning_sessions`` policy deletes a two-year-old session, the
    cascade removes its turns, traces, summaries and retrieval checks with the
    referencing table's owner privileges and no predicate at all. A hold placed
    on one of those rows would sit in the database looking exactly like
    protection and provide none, and the operator would discover that when the
    dispute it was placed for reached the point of asking for the data.

    So the refusal names the parent to hold instead. Tables with no retention
    window are refused too, with the more cheerful reason: nothing deletes them
    on a schedule, so a hold would be decoration.
    """
    if not reason.strip():
        raise HoldRefused("a hold needs a reason; §12.4's examples are all reasons "
                          "a human has to be able to re-read later")

    if table not in holdable_tables():
        parents = cascade_parents(table)
        if parents:
            raise HoldRefused(
                f"{table} is not deleted by the retention worker directly -- it "
                f"is reached by cascade from {', '.join(parents)}, and a "
                f"cascade consults no predicate. Hold the parent row in "
                f"{parents[0]} instead; it protects this row with it."
            )
        raise HoldRefused(
            f"{table} has no retention window (studium.jobs.retention.POLICIES), "
            f"so nothing deletes it on a schedule and a hold would protect it "
            f"from nothing. If it is deleted by a learner request, that is "
            f"§12.3's erasure path, which a hold deliberately does not block."
        )

    exists = session.execute(
        sql(f"SELECT 1 FROM {table} WHERE id = :id"), {"id": row_id}
    ).scalar()
    if not exists:
        # Refusing rather than accepting a hold on a row that is not there:
        # the usual cause is a typo'd id, and a hold on a nonexistent row is
        # indistinguishable from a hold that worked until someone goes looking.
        raise HoldRefused(f"no row {row_id} in {table}")

    return session.execute(
        sql(
            """
            INSERT INTO retention_holds (table_name, row_id, reason, placed_by)
            VALUES (:table, :row_id, :reason, :placed_by)
            ON CONFLICT (table_name, row_id) WHERE released_at IS NULL
            DO UPDATE SET reason = EXCLUDED.reason,
                          placed_by = EXCLUDED.placed_by
            RETURNING id
            """
        ),
        {
            "table": table,
            "row_id": row_id,
            "reason": reason.strip(),
            "placed_by": placed_by,
        },
    ).scalar_one()


def release_hold(
    session: Session, hold_id: uuid.UUID, *, reason: str = ""
) -> bool:
    """Retire a hold. The row stays (see the model docstring)."""
    result = session.execute(
        sql(
            """
            UPDATE retention_holds
               SET released_at = NOW(), released_reason = :reason
             WHERE id = :id AND released_at IS NULL
            """
        ),
        {"id": hold_id, "reason": reason.strip() or None},
    )
    return bool(result.rowcount)


def list_holds(session: Session, *, include_released: bool = False) -> list[dict]:
    rows = session.execute(
        sql(
            """
            SELECT h.id, h.table_name, h.row_id, h.reason, h.placed_at,
                   h.released_at, h.released_reason, u.email AS placed_by
              FROM retention_holds h
              LEFT JOIN users u ON u.id = h.placed_by
             WHERE (:all OR h.released_at IS NULL)
             ORDER BY h.placed_at DESC
            """
        ),
        {"all": include_released},
    ).all()
    return [dict(r._mapping) for r in rows]


def recent_actions(session: Session, *, limit: int = 50) -> list[dict]:
    rows = session.execute(
        sql(
            """
            SELECT ran_at, table_name, rows_deleted, duration_ms, metadata
              FROM retention_actions
             ORDER BY ran_at DESC
             LIMIT :limit
            """
        ),
        {"limit": limit},
    ).all()
    return [dict(r._mapping) for r in rows]


def iter_policy_tables() -> Iterator[str]:
    for policy in active_policies():
        yield policy.table
