"""Time the §8 access paths against a seeded database.

The claim being tested is §8's own, quoted exactly: the five session-start
queries "run in single-digit milliseconds on a warm cache at MVP scale". §14
asks for EXPLAIN ANALYZE with wall-clock "loosely bounded" -- there is no
numeric target in the specification, and this script does not invent one. It
reports what the queries actually cost and leaves the judgement to the reader.

Every statement below is copied verbatim from §8. Where a query is reworded to
run here, that is called out in a comment -- a measurement of a query the
application does not issue measures nothing.

    python scripts/seed_volume.py --tier mvp --reset
    python scripts/measure_queries.py --tier mvp
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import Connection  # noqa: E402

from studium.db import engine  # noqa: E402

WARMUP = 5
RUNS = 30

SESSION_START = {
    "1 learner + profile": """
        SELECT u.*, up.preferences, up.accessibility
        FROM users u
        LEFT JOIN user_profiles up ON up.user_id = u.id
        WHERE u.id = :user_id AND u.deleted_at IS NULL
    """,
    "2 active enrollments": """
        SELECT ls.*, s.title, s.slug, sm.concept_count, sm.load_bearing_count
        FROM learner_subjects ls
        JOIN subjects s ON s.id = ls.subject_id
        LEFT JOIN subject_metadata sm ON sm.subject_id = s.id
        WHERE ls.user_id = :user_id AND ls.archived_at IS NULL
    """,
    "3 open journal": """
        SELECT je.*
        FROM journal_entries je
        WHERE je.learner_subject_id = :learner_subject_id
          AND je.status IN ('open', 'partial')
        ORDER BY je.last_touched_at DESC
        LIMIT 20
    """,
    "4 due review cards": """
        SELECT rc.*, c.title, c.slug
        FROM review_cards rc
        JOIN concepts c ON c.id = rc.concept_id
        WHERE rc.learner_subject_id = :learner_subject_id
          AND rc.due_at <= NOW()
          AND NOT rc.suspended
        ORDER BY rc.due_at
        LIMIT 10
    """,
    "5 prior summary": """
        SELECT ss.*, session.mode, session.focus_concept_id
        FROM session_summaries ss
        JOIN learning_sessions session ON session.id = ss.session_id
        WHERE session.learner_subject_id = :learner_subject_id
          AND session.ended_at IS NOT NULL
        ORDER BY session.ended_at DESC
        LIMIT 1
    """,
}

OTHER_PATHS = {
    "unlock check": """
        WITH prereqs AS (
          SELECT ce.from_concept_id AS required
          FROM concept_edges ce
          WHERE ce.to_concept_id = :concept_id
            AND ce.kind = 'prerequisite'
        )
        SELECT
          COUNT(*) AS required_count,
          COUNT(*) FILTER (WHERE cm.p_known_decayed >= 0.85) AS satisfied_count
        FROM prereqs
        LEFT JOIN concept_mastery cm
          ON cm.concept_id = prereqs.required
         AND cm.learner_subject_id = :learner_subject_id
    """,
    "lecture segment": """
        SELECT ca.*, cc.source_chunk_id, sc.text AS source_text,
               s.title AS source_title, s.authors
        FROM content_artifacts ca
        LEFT JOIN content_citations cc ON cc.artifact_id = ca.id
        LEFT JOIN source_chunks sc ON sc.id = cc.source_chunk_id
        LEFT JOIN sources s ON s.id = sc.source_id
        WHERE ca.concept_id = :concept_id
          AND ca.kind = 'lecture_segment'
          AND ca.stance = :stance
          AND ca.status = 'active'
          AND ca.retired_at IS NULL
          AND ca.deleted_at IS NULL
        ORDER BY (ca.metadata->>'segment_index')::INT
    """,
    "cost budget check": """
        SELECT
          COALESCE(SUM(cost_usd) FILTER (WHERE day = CURRENT_DATE), 0) AS today,
          COALESCE(SUM(cost_usd) FILTER (
            WHERE day >= date_trunc('month', CURRENT_DATE)), 0) AS this_month,
          ubc.daily_hard_usd, ubc.monthly_hard_usd
        FROM cost_ledger cl
        RIGHT JOIN user_budget_caps ubc ON ubc.user_id = :user_id
        WHERE cl.user_id = :user_id OR cl.user_id IS NULL
        GROUP BY ubc.daily_hard_usd, ubc.monthly_hard_usd
    """,
}


@dataclass
class Result:
    label: str
    median_ms: float
    p95_ms: float
    max_ms: float
    rows: int
    seq_scan: bool


def _params(conn: Connection) -> dict:
    """Real identifiers from the seeded cohort.

    Deliberately the *busiest* learner rather than an arbitrary one: measuring
    the emptiest row in the table would flatter every number here.
    """
    row = conn.execute(
        text("""
            SELECT ls.user_id, ls.id AS learner_subject_id
              FROM learner_subjects ls
              JOIN learning_sessions s ON s.learner_subject_id = ls.id
             GROUP BY ls.user_id, ls.id
             ORDER BY count(s.id) DESC
             LIMIT 1
        """)
    ).one()
    concept_id = conn.execute(
        text("""
            SELECT to_concept_id FROM concept_edges
             WHERE kind = 'prerequisite' LIMIT 1
        """)
    ).scalar_one()
    return {
        "user_id": row.user_id,
        "learner_subject_id": row.learner_subject_id,
        "concept_id": concept_id,
        "stance": "default",
    }


def _bind(sql: str, params: dict) -> dict:
    """Only the parameters a given statement actually names."""
    return {k: v for k, v in params.items() if f":{k}" in sql}


def measure(conn: Connection, label: str, sql: str, params: dict) -> Result:
    bound = _bind(sql, params)
    statement = text(sql)

    rows = 0
    for _ in range(WARMUP):
        rows = len(conn.execute(statement, bound).fetchall())

    samples = []
    for _ in range(RUNS):
        started = time.perf_counter()
        conn.execute(statement, bound).fetchall()
        samples.append((time.perf_counter() - started) * 1000)

    plan = conn.execute(
        text(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}"), bound
    ).scalar_one()

    samples.sort()
    return Result(
        label=label,
        median_ms=statistics.median(samples),
        p95_ms=samples[int(len(samples) * 0.95) - 1],
        max_ms=samples[-1],
        rows=rows,
        seq_scan="Seq Scan" in str(plan),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", default="mvp", help="label for the report only")
    args = parser.parse_args()

    with engine.connect() as conn:
        params = _params(conn)
        counts = {
            t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
            for t in ("users", "session_turns", "agent_traces", "cost_ledger")
        }

        print(f"tier {args.tier!r}: " + ", ".join(
            f"{k}={v:,}" for k, v in counts.items()
        ))
        print(f"{WARMUP} warmup + {RUNS} timed runs per query, warm cache\n")

        header = f"{'query':<24}{'median':>9}{'p95':>9}{'max':>9}{'rows':>7}  plan"
        print(header)
        print("-" * len(header))

        session_start_total = 0.0
        for label, sql in SESSION_START.items():
            r = measure(conn, label, sql, params)
            session_start_total += r.median_ms
            flag = "seq scan" if r.seq_scan else ""
            print(
                f"{r.label:<24}{r.median_ms:>8.2f}m{r.p95_ms:>8.2f}m"
                f"{r.max_ms:>8.2f}m{r.rows:>7}  {flag}"
            )

        print("-" * len(header))
        print(f"{'session-start total':<24}{session_start_total:>8.2f}m")
        verdict = (
            "single-digit ms: CLAIM HOLDS"
            if session_start_total < 10
            else "EXCEEDS single-digit ms"
        )
        print(f"{'§8 claim':<24}{verdict}\n")

        for label, sql in OTHER_PATHS.items():
            r = measure(conn, label, sql, params)
            flag = "seq scan" if r.seq_scan else ""
            print(
                f"{r.label:<24}{r.median_ms:>8.2f}m{r.p95_ms:>8.2f}m"
                f"{r.max_ms:>8.2f}m{r.rows:>7}  {flag}"
            )


if __name__ == "__main__":
    main()
