"""Generate a synthetic database at a named scale tier, for measuring §8.

Spec §15 names three tiers -- MVP (3 users), classroom (30), university (500) --
so those are what this generates rather than a number invented here. §8 claims
the five session-start queries "run in single-digit milliseconds on a warm cache
at MVP scale"; measuring that claim, and its headroom at the larger tiers, is
what this exists for.

Set-based throughout: 500 learners is roughly 400,000 session turns, and
inserting those through the ORM would take longer than the measurement it
supports. Every statement is INSERT ... SELECT over generate_series.

Per-learner activity rates are assumptions, not spec -- the specification sizes
hardware and user counts but never says how much a learner does. They are stated
in ACTIVITY below so the numbers behind any measurement are legible, and so a
disagreement about them is a disagreement about one dict rather than about the
whole result.

    python scripts/seed_volume.py --tier mvp
    python scripts/seed_volume.py --tier university --reset
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from studium.db import SessionLocal  # noqa: E402

#: Spec §15's three sizing tiers.
TIERS = {"mvp": 3, "classroom": 30, "university": 500}


@dataclass(frozen=True)
class Activity:
    """Per-learner volume. Assumptions, not specification -- see module docstring.

    Calibrated to one term of use: a learner works through a subject of ~60
    concepts over ~40 sessions, leaves a journal trail, and accumulates review
    cards for everything they have seen.
    """

    concepts_per_subject: int = 60
    sessions: int = 40
    turns_per_session: int = 20
    mastery_events_per_concept: int = 8
    review_events_per_card: int = 12
    journal_entries: int = 30
    #: Fraction of journal entries left open -- the session-start query filters
    #: on status, so a table of entirely closed entries would not exercise it.
    open_fraction: float = 0.2
    cost_ledger_days: int = 90
    #: Sessions are spread across the study period rather than stacked in the
    #: last two days. An earlier version spaced them one hour apart, which put
    #: every session inside a 40-hour window and made the "recent activity"
    #: reads look faster than they will be.
    study_days: int = 90
    #: University tier only: learners who additionally ran a session today.
    #: Uniform spread understates the density that "today's activity" queries
    #: actually scan; this is the realistic worst case for them.
    busy_day_learners: int = 0
    busy_day_turns: int = 30


ACTIVITY = Activity()

#: Per-tier overrides. A year of ledger history at university scale because the
#: cost-check queries read a rolling window and should be measured against one.
TIER_ACTIVITY = {
    "university": Activity(cost_ledger_days=365, busy_day_learners=100),
}

# --- schema-shaped inserts -------------------------------------------------
# Ordered by dependency. Each is idempotent only in the sense that --reset
# truncates first; re-running without it stacks another cohort on top.

SUBJECT = """
INSERT INTO subjects (slug, title, short_description, status, version)
VALUES (:slug, 'Volume Test Subject', 'Synthetic, for measurement.', 'active', 1)
RETURNING id
"""

CONCEPTS = """
INSERT INTO concepts (subject_id, slug, title, depth, is_load_bearing,
                      estimated_minutes, position, metadata)
SELECT :subject_id,
       'vol-concept-' || g,
       'Volume Concept ' || g,
       (g % 5) + 1,
       (g % 4 = 0),
       20,
       g,
       '{"bkt": {"p_slip": 0.1, "p_guess": 0.2}}'::jsonb
  FROM generate_series(1, :n) g
"""

#: A linear chain plus a few skip edges, so prerequisite walks have depth to
#: traverse rather than a single hop.
EDGES = """
INSERT INTO concept_edges (subject_id, from_concept_id, to_concept_id, kind)
-- Casts are explicit because UNION resolves its column types before the
-- INSERT target is considered, so a bare literal arrives as text.
SELECT :subject_id, a.id, b.id, 'prerequisite'::concept_edge_kind
  FROM concepts a
  JOIN concepts b
    ON b.subject_id = a.subject_id
   AND b.position = a.position + 1
 WHERE a.subject_id = :subject_id
UNION ALL
SELECT :subject_id, a.id, b.id, 'related'::concept_edge_kind
  FROM concepts a
  JOIN concepts b
    ON b.subject_id = a.subject_id
   AND b.position = a.position + 3
 WHERE a.subject_id = :subject_id
   AND a.position % 5 = 0
ON CONFLICT DO NOTHING
"""

USERS = """
INSERT INTO users (email, display_name, role)
SELECT 'volume-' || g || '@studium.test', 'Volume Learner ' || g, 'learner'
  FROM generate_series(1, :n) g
"""

PROFILES = """
INSERT INTO user_profiles (user_id, stated_goals, preferences)
SELECT id, 'Measure things', '{"theme": "dark"}'::jsonb
  FROM users WHERE email LIKE 'volume-%@studium.test'
"""

BUDGET_CAPS = """
INSERT INTO user_budget_caps (user_id)
SELECT id FROM users WHERE email LIKE 'volume-%@studium.test'
"""

ENROLLMENTS = """
INSERT INTO learner_subjects (user_id, subject_id, subject_version,
                              syllabus_plan)
SELECT id, :subject_id, 1, '[]'::jsonb
  FROM users WHERE email LIKE 'volume-%@studium.test'
"""

MASTERY = """
INSERT INTO concept_mastery (learner_subject_id, concept_id,
                             p_known, p_known_decayed, evidence_count,
                             last_evidence_at, bkt_params)
SELECT ls.id, c.id,
       0.1 + (c.position % 8) * 0.1,
       0.1 + (c.position % 8) * 0.09,
       :per,
       NOW() - ((c.position % 30) || ' days')::interval,
       -- Spaces after the colons are load-bearing. A colon immediately
       -- followed by a digit is read by text() as a bind parameter; a colon
       -- followed by a space is literal JSON.
       '{"p_init": 0.1, "p_transit": 0.15, "p_slip": 0.1, "p_guess": 0.2}'::jsonb
  FROM learner_subjects ls
  JOIN concepts c ON c.subject_id = ls.subject_id
 WHERE ls.subject_id = :subject_id
"""

MASTERY_EVENTS = """
INSERT INTO mastery_events (concept_mastery_id, kind, p_known_before,
                            p_known_after, evidence)
SELECT cm.id, 'practice_correct'::mastery_event_kind, 0.4, 0.5,
       '{"synthetic": true}'::jsonb
  FROM concept_mastery cm
  JOIN learner_subjects ls ON ls.id = cm.learner_subject_id
 CROSS JOIN generate_series(1, :per) g
 WHERE ls.subject_id = :subject_id
"""

#: due_at straddles now() so the session-start "due cards" query has both
#: matching and non-matching rows to discriminate between.
REVIEW_CARDS = """
INSERT INTO review_cards (learner_subject_id, concept_id, due_at,
                          stability, difficulty, retrievability,
                          reps, lapses, state, suspended)
SELECT cm.learner_subject_id, cm.concept_id,
       NOW() + (((c.position % 21) - 10) || ' days')::interval,
       5.0, 5.0, 0.9, 3, 0, 'review', (c.position % 17 = 0)
  FROM concept_mastery cm
  JOIN concepts c ON c.id = cm.concept_id
  JOIN learner_subjects ls ON ls.id = cm.learner_subject_id
 WHERE ls.subject_id = :subject_id
"""

#: Requires a session, so this runs after SESSIONS. rating is a 1-4 integer,
#: not an enum.
REVIEW_EVENTS = """
INSERT INTO review_events (card_id, session_id, rating, elapsed_seconds,
                           stability_before, stability_after,
                           difficulty_before, difficulty_after)
SELECT rc.id, s.id, ((g % 4) + 1), 45, 5.0, 6.0, 5.0, 5.0
  FROM review_cards rc
  JOIN learner_subjects ls ON ls.id = rc.learner_subject_id
  JOIN LATERAL (
       SELECT id FROM learning_sessions
        WHERE learner_subject_id = ls.id
        ORDER BY started_at DESC LIMIT 1
  ) s ON TRUE
 CROSS JOIN generate_series(1, :per) g
 WHERE ls.subject_id = :subject_id
"""

SESSIONS = """
INSERT INTO learning_sessions (user_id, learner_subject_id, mode,
                               started_at, ended_at)
SELECT ls.user_id, ls.id,
       (ARRAY['lecture','tutorial','review','office_hours'])[(g % 4) + 1]
           ::session_mode,
       NOW() - ((g * :spacing) || ' hours')::interval,
       NOW() - ((g * :spacing) || ' hours')::interval + interval '35 minutes'
  FROM learner_subjects ls
 CROSS JOIN generate_series(1, :per) g
 WHERE ls.subject_id = :subject_id
"""

#: University tier: one 90-minute session today for a slice of the cohort.
BUSY_SESSIONS = """
INSERT INTO learning_sessions (user_id, learner_subject_id, mode,
                               started_at, ended_at)
SELECT ls.user_id, ls.id, 'lecture'::session_mode,
       NOW() - interval '3 hours', NOW() - interval '90 minutes'
  FROM (
       SELECT id, user_id FROM learner_subjects
        WHERE subject_id = :subject_id ORDER BY id LIMIT :busy
  ) ls
"""

#: Only sessions that have no turns yet, so this cannot double-fill the ones
#: TURNS already handled.
BUSY_TURNS = """
INSERT INTO session_turns (session_id, turn_index, actor, concept_id, input)
SELECT s.id, g - 1,
       (CASE WHEN g % 2 = 0 THEN 'learner' ELSE 'tutor' END)::agent_identity,
       c.id,
       '{"synthetic": true}'::jsonb
  FROM learning_sessions s
  JOIN learner_subjects ls ON ls.id = s.learner_subject_id
  JOIN LATERAL (
       SELECT id FROM concepts
        WHERE subject_id = :subject_id
        ORDER BY position
        OFFSET (abs(hashtext(s.id::text)) % :concepts) LIMIT 1
  ) c ON TRUE
 CROSS JOIN generate_series(1, :per) g
 WHERE ls.subject_id = :subject_id
   AND NOT EXISTS (SELECT 1 FROM session_turns t WHERE t.session_id = s.id)
"""

TURNS = """
INSERT INTO session_turns (session_id, turn_index, actor, concept_id, input)
SELECT s.id, g - 1,
       (CASE WHEN g % 2 = 0 THEN 'learner' ELSE 'tutor' END)::agent_identity,
       c.id,
       '{"synthetic": true}'::jsonb
  FROM learning_sessions s
  JOIN learner_subjects ls ON ls.id = s.learner_subject_id
  JOIN LATERAL (
       SELECT id FROM concepts
        WHERE subject_id = :subject_id
        ORDER BY position
        OFFSET (abs(hashtext(s.id::text)) % :concepts) LIMIT 1
  ) c ON TRUE
 CROSS JOIN generate_series(1, :per) g
 WHERE ls.subject_id = :subject_id
"""

TRACES = """
INSERT INTO agent_traces (session_turn_id, user_id, agent, model,
                          prompt_messages, system_prompt_hash, latency_ms,
                          cost_usd, tokens_in, tokens_out, cache_read_tokens,
                          cache_write_5m_tokens, cache_write_1h_tokens)
SELECT t.id, s.user_id, t.actor, 'claude-opus-4-8',
       '[]'::jsonb, repeat('a', 64), 400, 0.004, 900, 250, 400, 30, 0
  FROM session_turns t
  JOIN learning_sessions s ON s.id = t.session_id
  JOIN learner_subjects ls ON ls.id = s.learner_subject_id
 WHERE ls.subject_id = :subject_id
   AND t.actor <> 'learner'
   AND NOT EXISTS (
       SELECT 1 FROM agent_traces a WHERE a.session_turn_id = t.id
   )
"""

SUMMARIES = """
INSERT INTO session_summaries (session_id, summary, key_points, open_threads,
                               generated_by, cost_usd)
SELECT s.id, 'Synthetic summary for measurement.',
       '["point one","point two"]'::jsonb, '[]'::jsonb,
       'orchestrator'::agent_identity, 0.02
  FROM learning_sessions s
  JOIN learner_subjects ls ON ls.id = s.learner_subject_id
 WHERE ls.subject_id = :subject_id
   AND NOT EXISTS (
       SELECT 1 FROM session_summaries ss WHERE ss.session_id = s.id
   )
"""

JOURNAL = """
INSERT INTO journal_entries (learner_subject_id, user_id, concept_id, status,
                             summary, hypothesis, origin, last_touched_at)
SELECT ls.id, ls.user_id, c.id,
       (CASE WHEN g <= :open THEN 'open' ELSE 'resolved' END)::journal_status,
       'Synthetic observation ' || g, 'Synthetic hypothesis',
       'tracker_inferred',
       NOW() - ((g) || ' hours')::interval
  FROM learner_subjects ls
  JOIN LATERAL (
       SELECT id FROM concepts
        WHERE subject_id = :subject_id
        ORDER BY position
        OFFSET (abs(hashtext(ls.id::text)) % :concepts) LIMIT 1
  ) c ON TRUE
 CROSS JOIN generate_series(1, :per) g
 WHERE ls.subject_id = :subject_id
"""

ARTIFACTS = """
INSERT INTO content_artifacts (concept_id, kind, title, body, generated_by,
                               model, status, metadata)
SELECT c.id, 'lecture_segment', 'Segment ' || g,
       repeat('Synthetic lecture prose. ', 40),
       'lecturer', 'claude-opus-4-8', 'active',
       jsonb_build_object('segment_index', g - 1)
  FROM concepts c
 CROSS JOIN generate_series(1, 5) g
 WHERE c.subject_id = :subject_id
"""

LEDGER = """
INSERT INTO cost_ledger (user_id, day, model, tokens_in, tokens_out,
                         cache_read_tokens, cost_agent_usd, session_count)
SELECT u.id, (CURRENT_DATE - g), 'claude-opus-4-8',
       50000, 12000, 30000, 1.25, 2
  FROM users u
 CROSS JOIN generate_series(0, :days - 1) g
 WHERE u.email LIKE 'volume-%@studium.test'
ON CONFLICT DO NOTHING
"""

COUNTED = (
    "users", "learner_subjects", "concepts", "concept_edges", "concept_mastery",
    "mastery_events", "review_cards", "review_events", "learning_sessions",
    "session_turns", "agent_traces", "session_summaries", "journal_entries",
    "content_artifacts", "cost_ledger",
)

#: Truncate order does not matter with CASCADE, but naming every table keeps
#: --reset honest: it removes the whole synthetic cohort, not part of it.
RESET = f"TRUNCATE {', '.join(COUNTED)} RESTART IDENTITY CASCADE"


def seed(session: Session, learners: int, activity: Activity = ACTIVITY) -> dict:
    subject_id = session.execute(
        text(SUBJECT), {"slug": f"volume-test-{learners}"}
    ).scalar_one()
    base = {"subject_id": subject_id}
    concepts = activity.concepts_per_subject

    steps = [
        ("concepts", CONCEPTS, {"n": concepts}),
        ("edges", EDGES, {}),
        ("users", USERS, {"n": learners}),
        ("profiles", PROFILES, {}),
        ("budget caps", BUDGET_CAPS, {}),
        ("enrollments", ENROLLMENTS, {}),
        ("mastery", MASTERY, {"per": activity.mastery_events_per_concept}),
        ("mastery events", MASTERY_EVENTS, {"per": activity.mastery_events_per_concept}),
        # Sessions precede review events, which need a session to attach to.
        (
            "sessions",
            SESSIONS,
            {
                "per": activity.sessions,
                # Spread across the study period, not stacked in recent hours.
                "spacing": max(
                    1, round(activity.study_days * 24 / max(1, activity.sessions))
                ),
            },
        ),
        ("turns", TURNS, {"per": activity.turns_per_session, "concepts": concepts}),
        *(
            [
                ("busy sessions", BUSY_SESSIONS, {"busy": activity.busy_day_learners}),
                (
                    "busy turns",
                    BUSY_TURNS,
                    {"per": activity.busy_day_turns, "concepts": concepts},
                ),
            ]
            if activity.busy_day_learners
            else []
        ),
        ("traces", TRACES, {}),
        ("summaries", SUMMARIES, {}),
        ("review cards", REVIEW_CARDS, {}),
        ("review events", REVIEW_EVENTS, {"per": activity.review_events_per_card}),
        (
            "journal",
            JOURNAL,
            {
                "per": activity.journal_entries,
                "open": max(1, int(activity.journal_entries * activity.open_fraction)),
                "concepts": concepts,
            },
        ),
        ("artifacts", ARTIFACTS, {}),
        ("ledger", LEDGER, {"days": activity.cost_ledger_days}),
    ]

    for label, sql, params in steps:
        started = time.perf_counter()
        session.execute(text(sql), {**base, **params})
        print(f"  {label:16} {time.perf_counter() - started:6.2f}s", flush=True)

    session.commit()
    return {"subject_id": subject_id}


def counts(session: Session) -> dict[str, int]:
    return {
        table: session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        for table in COUNTED
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=sorted(TIERS), default="mvp")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="truncate every seeded table first (destructive)",
    )
    args = parser.parse_args()
    learners = TIERS[args.tier]
    activity = TIER_ACTIVITY.get(args.tier, ACTIVITY)

    with SessionLocal() as session:
        if args.reset:
            session.execute(text(RESET))
            session.commit()
            print("reset: all seeded tables truncated")

        print(f"seeding tier {args.tier!r} ({learners} learners)")
        if activity is not ACTIVITY:
            print(
                f"  tier overrides: ledger {activity.cost_ledger_days}d, "
                f"busy-day learners {activity.busy_day_learners}"
            )
        started = time.perf_counter()
        seed(session, learners, activity)
        elapsed = time.perf_counter() - started

        print(f"\nseeded in {elapsed:.1f}s\n")
        print("row counts")
        for table, n in counts(session).items():
            print(f"  {table:22} {n:>9,}")

        # A measurement against stale statistics measures the planner's
        # ignorance, not the schema.
        print("\nANALYZE...", flush=True)
        session.execute(text("ANALYZE"))
        session.commit()
        print("done")


if __name__ == "__main__":
    main()
