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
    Policy("learner_subjects", None, note="added: deleted on erasure only"),
    Policy("users", None, note="soft delete on close; hard delete after 30 days"),
)


def apply_retention(session: Session, *, dry_run: bool = False) -> dict[str, int]:
    """Delete rows past their window. Returns per-table counts.

    Weekly job. Ordered so that cascading parents run before the children they
    would have cascaded into, which keeps the counts meaningful.
    """
    counts: dict[str, int] = {}
    for policy in POLICIES:
        if policy.window is None or policy.predicate is None:
            continue
        verb = "SELECT count(*) FROM" if dry_run else "DELETE FROM"
        sql = text(f"{verb} {policy.table} WHERE {policy.predicate}")
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
