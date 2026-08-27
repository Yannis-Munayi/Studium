"""Retention windows (spec v1.1 §10).

The right-to-erasure procedure lives in ``studium.privacy``, which is where
v1.1 §10 names it. This module owns only the time-based windows.

Runs as ``studium_owner``: every table touched here is one migration 0003
revokes DELETE on from the application role, which is the point -- history is
append-only to the app and prunable only by a scheduled, audited job.

The retention table in §10 omits ten tables while claiming to cover every one.
The policies below are complete; the additions are marked. See DIVERGENCES.md
(B7).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class Policy:
    table: str
    #: None means "kept indefinitely".
    window: dt.timedelta | None
    #: SQL fragment selecting rows eligible for deletion.
    predicate: str | None = None
    note: str = ""


DAY = dt.timedelta(days=1)
YEAR = dt.timedelta(days=365)

POLICIES: tuple[Policy, ...] = (
    Policy(
        "auth_sessions",
        30 * DAY,
        "expires_at < NOW() - INTERVAL '30 days'",
        "purged relative to expiry, not creation",
    ),
    Policy(
        "agent_traces",
        90 * DAY,
        # Traces linked to an open review item are kept: §10 says "retained
        # longer only if linked to a review queue item", reachable through the
        # turn the trace belongs to.
        """
        created_at < NOW() - INTERVAL '90 days'
        AND NOT EXISTS (
            SELECT 1 FROM content_review_queue q
             WHERE q.session_turn_id = agent_traces.session_turn_id
        )
        """,
        "full prompts carry privacy and storage cost",
    ),
    Policy(
        "learning_sessions",
        2 * YEAR,
        "started_at < NOW() - INTERVAL '2 years'",
        "cascades to turns, traces, review events, retrieval checks, summaries",
    ),
    Policy(
        "mastery_events",
        5 * YEAR,
        "created_at < NOW() - INTERVAL '5 years'",
        "the estimate itself persists in concept_mastery",
    ),
    Policy("review_events", 2 * YEAR, "created_at < NOW() - INTERVAL '2 years'"),
    Policy("cost_ledger", 3 * YEAR, "day < CURRENT_DATE - INTERVAL '3 years'"),
    Policy(
        "audit_log", 7 * YEAR, "created_at < NOW() - INTERVAL '7 years'", "regulatory margin"
    ),
    Policy(
        "ingestion_jobs",
        90 * DAY,
        "finished_at IS NOT NULL AND finished_at < NOW() - INTERVAL '90 days'",
    ),
    Policy(
        "content_review_queue",
        YEAR,
        "resolved_at IS NOT NULL AND resolved_at < NOW() - INTERVAL '1 year'",
    ),
    Policy(
        "ingestion_review_queue",
        YEAR,
        # Ingestion §12.3: "the row persists for audit; a retention job deletes
        # resolved rows after 1 year." Keyed on resolved_at, so a *pending*
        # item is never aged out however long it has been waiting -- an
        # unresolved license question does not become resolved by being
        # ignored, and a queue that quietly drops its oldest items is worse
        # than one that grows.
        "resolved_at IS NOT NULL AND resolved_at < NOW() - INTERVAL '1 year'",
        "mirrors content_review_queue; pending items are never aged out",
    ),
    # --- not in the spec's table, added for completeness -------------------
    Policy(
        "retrieval_checks",
        2 * YEAR,
        "created_at < NOW() - INTERVAL '2 years'",
        "added: cascades from learning_sessions anyway, explicit for clarity",
    ),
    Policy("journal_events", None, note="kept for full journal history"),
    Policy("journal_entries", None, note="deleted on learner request only"),
    Policy("assessment_attempts", None, note="kept as portfolio evidence"),
    Policy("assessment_responses", None, note="kept as portfolio evidence"),
    Policy("portfolio_items", None, note="kept until learner deletion"),
    Policy("session_summaries", None, note="added: cascades with its session"),
    Policy("concept_mastery", None, note="added: current state, never aged out"),
    # Same category as concept_mastery: the live FSRS schedule, not a record of
    # past reviews. Ageing a card out would silently drop a concept from the
    # review rotation while its mastery row still claims it is being tracked.
    # It leaves with the enrollment, by cascade.
    Policy("review_cards", None, note="added: current schedule, never aged out"),
    Policy("learner_subjects", None, note="added: deleted on erasure only"),
    Policy("users", None, note="soft delete on close; hard delete after 30 days"),
    # --- evaluation harness (evaluation spec §5, §7.3, §12.3) --------------
    #
    # All five are kept indefinitely, and each for its own reason rather than
    # by default.
    Policy(
        "golden_datasets",
        None,
        note="evaluation §7.3: retired via active=FALSE, never deleted, so "
        "old results stay comparable",
    ),
    Policy(
        "golden_dataset_entries",
        None,
        note="evaluation §7.3: 'entries are versioned but not deleted'; "
        "evaluation_results.entry_id is ON DELETE RESTRICT besides",
    ),
    Policy(
        "evaluation_runs",
        None,
        # A window here would delete the baseline §13.1 step 4 compares
        # against. The suite is ~250 entries run weekly: a few thousand rows a
        # year, against a gate that stops silently passing if they expire.
        note="the regression baseline; ageing it out disarms the §13.2 gate",
    ),
    Policy(
        "evaluation_results",
        None,
        note="per-entry history behind the baseline; §7.3 keeps retired "
        "datasets' results 'queryable for historical comparison'",
    ),
    Policy(
        "signing_keys",
        None,
        # §12.3: "Old public keys remain published so historical portfolio
        # items stay verifiable." Deleting a retired key invalidates every
        # credential it ever signed -- a verifier could no longer tell a
        # rotated key from a forged one.
        note="evaluation §12.3: retired keys stay published forever",
    ),
    # --- infrastructure §12 (migration 0012) --------------------------------
    #
    # Both kept indefinitely, and the first one is the interesting case: it is
    # the retention worker's own audit trail, and giving it a window would mean
    # the worker deleting the evidence of its own deletions on a schedule.
    Policy(
        "retention_actions",
        None,
        # §12.2: "Audit trail for any question of 'why did that data go away'."
        # A window here would answer that question only for the recent past,
        # and the questions that reach an audit trail are rarely recent. One
        # row per policy per night is ~4,000 a year -- kilobytes.
        note="infrastructure §12.2: the audit trail the worker writes; a "
        "window would have the worker prune its own evidence",
    ),
    Policy(
        "retention_holds",
        None,
        # A hold is released, never deleted, precisely so the record that data
        # was deliberately kept survives. Ageing the row out would destroy
        # that record -- and would do it for exactly the holds that are old
        # enough for the dispute to have reached a court.
        note="infrastructure §12.4: released holds stay as the record that "
        "the data was kept, which is what a dispute later asks about",
    ),
)


#: Policies the worker actually executes: a window and a predicate. The rest of
#: POLICIES documents tables that are deliberately never aged out, and a
#: reviewer reading "kept indefinitely" is the point of their being there.
def active_policies() -> tuple[Policy, ...]:
    return tuple(p for p in POLICIES if p.window is not None and p.predicate is not None)


#: Excludes rows under an unreleased retention hold (infrastructure §12.4).
#:
#: Applied inside :func:`deletion_predicate` rather than left to each caller,
#: so ``apply_retention`` and the scheduled worker in ``studium.ops.retention``
#: cannot disagree about what is protected. A hold the weekly job honours and
#: the nightly job does not is worse than no hold at all -- the data goes, and
#: the row saying it was being kept is still there.
HOLD_EXCLUSION = """
    NOT EXISTS (
        SELECT 1 FROM retention_holds h
         WHERE h.table_name = '{table}'
           AND h.row_id = {table}.id
           AND h.released_at IS NULL
    )
"""


def deletion_predicate(policy: Policy) -> str:
    """The policy's own predicate, ANDed with the retention-hold exclusion."""
    assert policy.predicate is not None
    return (
        f"({policy.predicate.strip()})"
        f" AND {HOLD_EXCLUSION.format(table=policy.table).strip()}"
    )


def apply_retention(session: Session, *, dry_run: bool = False) -> dict[str, int]:
    """Delete rows past their window. Returns per-table counts.

    One statement per policy, ordered so that cascading parents run before the
    children they would have cascaded into, which keeps the counts meaningful.

    The scheduled path is :func:`studium.ops.retention.run_retention`, which
    adds batching, a per-policy transaction and the §12.2 audit row. This
    remains the direct form: it is what the integrity tests drive, and what an
    operator runs when they want one pass with nothing else attached.
    """
    counts: dict[str, int] = {}
    for policy in active_policies():
        verb = "SELECT count(*) FROM" if dry_run else "DELETE FROM"
        sql = text(f"{verb} {policy.table} WHERE {deletion_predicate(policy)}")
        result = session.execute(sql)
        counts[policy.table] = (
            int(result.scalar_one()) if dry_run else int(result.rowcount or 0)
        )
    if not dry_run:
        session.commit()
    return counts


def check_dangling_chunk_refs(session: Session) -> list[uuid.UUID]:
    """Daily consistency check for ``concept_sources.chunk_ids`` (§6.2).

    The array cannot carry a foreign key, so broken references degrade
    gracefully rather than blocking a lecture. This flags them for review.
    """
    sql = text(
        """
        SELECT cs.id
          FROM concept_sources cs
         WHERE EXISTS (
             SELECT 1
               FROM unnest(cs.chunk_ids) AS cid
              WHERE NOT EXISTS (SELECT 1 FROM source_chunks sc WHERE sc.id = cid)
         )
        """
    )
    return list(session.execute(sql).scalars())
