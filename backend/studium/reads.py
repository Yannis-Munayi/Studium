"""Learner-facing read paths (frontend §6.1, §6.4, §6.5, §12).

The desk, the journal browser, the entry detail and the session-close modal all
read rows the data layer already ships and the runtime already writes. What was
missing was any way to get them out of the database: ``studium/api/app.py``
served the session lifecycle, the interrupt channel and citation resolution, and
nothing else. Three of the five MVP surfaces had no server (frontend
DIVERGENCES F4).

Everything here is a read except :func:`patch_journal_entry`, and every function
takes ``user_id`` and filters on it. That is not belt and braces: there is no
authentication behind the API (frontend F7, `lib/auth.ts`), so scoping is the
only thing standing between one learner's journal and another's, and a query
that forgot it would leak silently rather than fail.

Shapes are the ones ``studium-web/lib/api/schemas.ts`` parses. Where a column
and a field disagree -- ``origin`` versus ``summary_author``, a decayed mastery
value versus a stored one -- the translation happens here, once, rather than in
five components.

Returns plain dicts throughout, per :mod:`studium.asyncdb`'s rule: an ORM
instance handed back across the thread boundary lazy-loads after its session has
closed, and raises somewhere far from the cause.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.mastery import DECAY_HALF_LIFE

log = logging.getLogger(__name__)

#: §6.1: "Open journal entries (top 5)", "Last 5 closed sessions",
#: "shows the next 3 concepts".
DESK_JOURNAL_LIMIT = 5
DESK_SESSION_LIMIT = 5
DESK_SYLLABUS_LIMIT = 3

#: The desk's mastery summary is a reduction, not the concept graph (§6.1).
#: Twelve rows is what the bar list can show without becoming the map it stands
#: in for.
DESK_MASTERY_LIMIT = 12

#: §6.4's default window when the client does not send one.
JOURNAL_DEFAULT_DAYS = 30
JOURNAL_LIMIT = 200

#: **Whether the Confusion-Tracker's hypothesis is serialised to the learner.**
#:
#: Two specifications disagree here and both are explicit. Data layer §11, as
#: implemented in ``studium.acl.project_journal_entry``, says the hypothesis is
#: "a Tutor-facing prompt aid ... never serialised to the learner" -- because it
#: can be wrong and reading it can be dispiriting. Frontend §6.4 and §12.1 ask
#: for it on the entry list and the detail view, read-only, behind a "this is
#: the system's inference, not your words" framing built precisely to answer
#: that concern.
#:
#: Left off, because a privacy projection that already shipped is not something
#: to overturn from the endpoint that would benefit from overturning it. The
#: frontend renders the hypothesis only ``if (entry.hypothesis)``, so it
#: degrades to exactly §6.4's "if it exists" branch with nothing to change
#: there. Recorded as F16; flipping this constant is the whole reversal.
SERVE_HYPOTHESIS_TO_LEARNER = False


class NotFound(LookupError):
    """The row does not exist, or does not belong to this learner.

    One exception for both, deliberately. Distinguishing them at the HTTP
    boundary would answer "does journal entry X exist" for an id the caller does
    not own, which is the question the scoping is there to refuse.
    """


# --- shared fragments ------------------------------------------------------

#: Decay recomputed at read time rather than read from ``p_known_decayed``.
#: The stored column is only as fresh as the last decay job, and the Curator,
#: the gate and ``suggest_next_unlocked`` all recompute -- a desk showing the
#: stale number while every decision used the fresh one would be a summary of a
#: state the system was not in. See DIVERGENCES (C1).
_DECAYED = """
    CASE WHEN cm.last_evidence_at IS NULL THEN cm.p_known
         ELSE cm.p_known * pow(
                 0.5,
                 EXTRACT(EPOCH FROM (NOW() - cm.last_evidence_at))
                 / :half_life_seconds
              )
    END
"""

#: Every column the journal shapes need, plus the concept title they render.
_JOURNAL_SELECT = """
    SELECT j.id,
           j.learner_subject_id,
           j.concept_id,
           c.title AS concept_name,
           j.status,
           j.summary,
           j.origin,
           j.hypothesis,
           j.learner_note,
           j.first_seen_at,
           j.last_touched_at
      FROM journal_entries j
      LEFT JOIN concepts c ON c.id = j.concept_id
"""


def _journal_row(row: Any) -> dict[str, Any]:
    """One ``journal_entries`` row in the shape the client parses.

    ``summary_author`` is derived rather than stored: the table records
    ``origin`` (data layer §6.7), and only ``learner_flagged`` means the learner
    wrote the summary themselves. §12.1 needs the distinction to attribute the
    text, and inventing a column for it would duplicate a fact ``origin``
    already carries.
    """
    return {
        "id": str(row.id),
        "learner_subject_id": str(row.learner_subject_id),
        "concept_id": str(row.concept_id) if row.concept_id else None,
        "concept_name": row.concept_name,
        "status": row.status,
        "summary": row.summary,
        "summary_author": "learner" if row.origin == "learner_flagged" else "tutor",
        "hypothesis": row.hypothesis if SERVE_HYPOTHESIS_TO_LEARNER else None,
        "learner_note": row.learner_note,
        "first_seen_at": _iso(row.first_seen_at),
        "last_touched_at": _iso(row.last_touched_at),
    }


# --- the desk (§6.1) -------------------------------------------------------


def desk(session: Session, user_id: uuid.UUID) -> dict[str, Any]:
    """Everything §6.1 shows, in one round trip.

    One endpoint rather than five because the desk is a single glance -- five
    requests would render it in five stages, each shifting the layout under
    someone who is reading it.
    """
    learner = _learner(session, user_id)
    if learner is None:
        raise NotFound(f"no learner {user_id}")

    enrollment = _primary_enrollment(session, user_id)
    sessions = _recent_sessions(session, user_id)

    return {
        "learner": learner,
        "open_session": next((s for s in sessions if s["ended_at"] is None), None),
        "recent_sessions": [s for s in sessions if s["ended_at"] is not None][
            :DESK_SESSION_LIMIT
        ],
        "syllabus_next": _syllabus_next(session, enrollment),
        "open_journal_entries": journal_entries(
            session,
            user_id,
            statuses=["open", "partial"],
            since=None,
            limit=DESK_JOURNAL_LIMIT,
        ),
        "mastery": _mastery(session, user_id),
    }


def _learner(session: Session, user_id: uuid.UUID) -> dict[str, Any] | None:
    row = session.execute(
        sql(
            """
            SELECT u.id, u.display_name, u.timezone,
                   COALESCE(p.preferences, '{}'::jsonb) AS preferences
              FROM users u
              LEFT JOIN user_profiles p ON p.user_id = u.id
             WHERE u.id = :user_id AND u.deleted_at IS NULL
            """
        ),
        {"user_id": user_id},
    ).first()
    if row is None:
        return None

    preferences = row.preferences or {}
    return {
        "id": str(row.id),
        "display_name": row.display_name,
        "timezone": row.timezone,
        # Preference keys the frontend reads. Absent means the default, not an
        # error: `user_profiles.preferences` is deliberately unconstrained so a
        # new key needs no migration (data layer §6.1).
        "default_session_minutes": int(preferences.get("default_session_minutes", 90)),
        "show_cost": bool(preferences.get("show_cost", False)),
    }


def _primary_enrollment(session: Session, user_id: uuid.UUID) -> Any:
    """The enrollment the desk speaks for.

    MVP has one active subject per learner. When that stops being true this is
    the first thing that has to become a choice rather than a lookup -- named
    here so the assumption is visible instead of implied by an ORDER BY.
    """
    return session.execute(
        sql(
            """
            SELECT id, syllabus_plan, current_focus_concept_id
              FROM learner_subjects
             WHERE user_id = :user_id AND archived_at IS NULL
             ORDER BY enrolled_at DESC
             LIMIT 1
            """
        ),
        {"user_id": user_id},
    ).first()


def _recent_sessions(session: Session, user_id: uuid.UUID) -> list[dict[str, Any]]:
    """The open session (if any) and the last closed ones (§6.1).

    ``concepts_touched`` prefers ``session_summaries``, which §6.1 names as the
    source. An open session has no summary yet, so it falls back to the distinct
    concepts its turns carry -- otherwise the continue-card, the one thing on
    this surface a learner is likely to click, would be the only row with no
    subject line.
    """
    rows = session.execute(
        sql(
            """
            SELECT s.id,
                   s.mode,
                   s.started_at,
                   s.ended_at,
                   EXTRACT(EPOCH FROM (COALESCE(s.ended_at, NOW()) - s.started_at))
                       / 60.0 AS duration_minutes,
                   COALESCE(
                       (SELECT array_agg(c.title ORDER BY c.title)
                          FROM concepts c
                         WHERE c.id = ANY(ss.concepts_touched)),
                       (SELECT array_agg(DISTINCT c2.title)
                          FROM session_turns t
                          JOIN concepts c2 ON c2.id = t.concept_id
                         WHERE t.session_id = s.id),
                       ARRAY[]::text[]
                   ) AS concepts_touched
              FROM learning_sessions s
              LEFT JOIN session_summaries ss ON ss.session_id = s.id
             WHERE s.user_id = :user_id
             ORDER BY s.started_at DESC
             LIMIT :limit
            """
        ),
        # One more than the five closed rows §6.1 asks for, so an open session
        # occupying the newest slot cannot push the fifth closed one out.
        {"user_id": user_id, "limit": DESK_SESSION_LIMIT + 1},
    ).all()

    return [
        {
            "id": str(row.id),
            "mode": row.mode,
            "started_at": _iso(row.started_at),
            "ended_at": _iso(row.ended_at),
            "duration_minutes": (
                float(row.duration_minutes) if row.duration_minutes is not None else None
            ),
            "concepts_touched": list(row.concepts_touched or []),
        }
        for row in rows
    ]


def _syllabus_next(session: Session, enrollment: Any) -> list[dict[str, Any]]:
    """§6.1's "next 3 concepts" from ``learner_subjects.syllabus_plan``.

    The plan is JSONB and the Curator writes it; the column's comment calls it
    "the Curator's ordered list of concept ids" but nothing constrains the shape,
    so both a bare id list and a list of objects are read. A plan entry that does
    not resolve to a concept is skipped rather than rendered as a blank line --
    the syllabus moving under a stored plan is expected (subject_version exists
    for exactly that), and it is not the desk's job to report it.
    """
    if enrollment is None:
        return []

    ids = _plan_concept_ids(enrollment.syllabus_plan)
    if not ids:
        return []

    # Start at the current focus: what is "coming up" is what follows where the
    # learner is, not the head of a plan they are already three concepts into.
    focus = enrollment.current_focus_concept_id
    if focus is not None and focus in ids:
        ids = ids[ids.index(focus) + 1 :]

    ids = ids[: DESK_SYLLABUS_LIMIT * 2]
    if not ids:
        return []

    titles = {
        row.id: row.title
        for row in session.execute(
            sql("SELECT id, title FROM concepts WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": ids},
        ).all()
    }
    resolved = [
        {"id": str(cid), "name": titles[cid]} for cid in ids if cid in titles
    ]
    return resolved[:DESK_SYLLABUS_LIMIT]


def _plan_concept_ids(plan: Any) -> list[uuid.UUID]:
    if not isinstance(plan, list):
        return []
    out: list[uuid.UUID] = []
    for item in plan:
        raw = item
        if isinstance(item, dict):
            raw = item.get("concept_id") or item.get("id")
        try:
            out.append(uuid.UUID(str(raw)))
        except (ValueError, AttributeError, TypeError):
            log.debug("syllabus_plan entry is not a concept id: %r", item)
    return out


def _mastery(session: Session, user_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = session.execute(
        sql(
            f"""
            SELECT cm.concept_id,
                   c.title AS concept_name,
                   cm.p_known,
                   {_DECAYED} AS p_known_decayed
              FROM concept_mastery cm
              JOIN concepts c ON c.id = cm.concept_id
              JOIN learner_subjects ls ON ls.id = cm.learner_subject_id
             WHERE ls.user_id = :user_id AND ls.archived_at IS NULL
             ORDER BY {_DECAYED} DESC
             LIMIT :limit
            """  # noqa: S608 -- _DECAYED is a module constant, not input
        ),
        {
            "user_id": user_id,
            "limit": DESK_MASTERY_LIMIT,
            "half_life_seconds": DECAY_HALF_LIFE.total_seconds(),
        },
    ).all()

    return [
        {
            "concept_id": str(row.concept_id),
            "concept_name": row.concept_name,
            "p_known": float(row.p_known),
            "p_known_decayed": float(row.p_known_decayed),
        }
        for row in rows
    ]


# --- the journal (§6.4) ----------------------------------------------------


def journal_entries(
    session: Session,
    user_id: uuid.UUID,
    *,
    statuses: list[str] | None = None,
    subject_ids: list[uuid.UUID] | None = None,
    concept_ids: list[uuid.UUID] | None = None,
    since: dt.datetime | None = None,
    search: str | None = None,
    limit: int = JOURNAL_LIMIT,
) -> list[dict[str, Any]]:
    """§6.4's filtered entry list, across every subject the learner is in.

    Not scoped to one enrollment: §6.4 is explicit that the journal is "all
    open, partial, and resolved entries **across the learner's subjects**". The
    subject filter is a filter, not a required parameter -- which also means the
    surface renders before anything has told it which enrollment to ask about.

    ``search`` is a substring match, per §6.4: "Not fuzzy search -- a substring
    match is sufficient at MVP scale." It runs over the summary and the
    learner's own note, which are the two fields they wrote or can edit; the
    hypothesis is excluded because it is not shown (see
    :data:`SERVE_HYPOTHESIS_TO_LEARNER`) and matching on invisible text produces
    results a learner cannot account for.
    """
    clauses = ["j.user_id = :user_id"]
    params: dict[str, Any] = {"user_id": user_id, "limit": limit}

    if statuses:
        clauses.append("j.status = ANY(CAST(:statuses AS journal_status[]))")
        params["statuses"] = statuses
    if subject_ids:
        clauses.append("j.learner_subject_id = ANY(CAST(:subject_ids AS uuid[]))")
        params["subject_ids"] = subject_ids
    if concept_ids:
        clauses.append("j.concept_id = ANY(CAST(:concept_ids AS uuid[]))")
        params["concept_ids"] = concept_ids
    if since is not None:
        clauses.append("j.last_touched_at >= :since")
        params["since"] = since
    if search:
        clauses.append("(j.summary ILIKE :search OR j.learner_note ILIKE :search)")
        params["search"] = f"%{search}%"

    rows = session.execute(
        sql(
            _JOURNAL_SELECT
            + f" WHERE {' AND '.join(clauses)}"  # noqa: S608 -- fragments are literals
            + " ORDER BY j.last_touched_at DESC LIMIT :limit"
        ),
        params,
    ).all()
    return [_journal_row(row) for row in rows]


def journal_entry(
    session: Session, user_id: uuid.UUID, entry_id: uuid.UUID
) -> dict[str, Any]:
    """One entry with §6.4's history timeline."""
    row = session.execute(
        sql(_JOURNAL_SELECT + " WHERE j.id = :entry_id AND j.user_id = :user_id"),
        {"entry_id": entry_id, "user_id": user_id},
    ).first()
    if row is None:
        raise NotFound(f"no journal entry {entry_id}")

    events = session.execute(
        sql(
            """
            SELECT id, kind, created_at, session_id
              FROM journal_events
             WHERE entry_id = :entry_id
             ORDER BY created_at
            """
        ),
        {"entry_id": entry_id},
    ).all()

    return {
        **_journal_row(row),
        "history": [
            {
                "id": str(event.id),
                "kind": event.kind,
                "at": _iso(event.created_at),
                "session_id": str(event.session_id) if event.session_id else None,
            }
            for event in events
        ],
    }


#: The ``journal_event_kind`` a status change records (data layer §6.7).
#: ``open`` is deliberately absent from a first transition and present as
#: ``reopened`` -- an entry only becomes open again, never open for the first
#: time, by a learner's action.
_STATUS_EVENT: dict[str, str] = {
    "open": "reopened",
    "partial": "partially_addressed",
    "resolved": "resolved",
    "archived": "archived",
}


def patch_journal_entry(
    session: Session,
    user_id: uuid.UUID,
    entry_id: uuid.UUID,
    *,
    status: str | None = None,
    summary: str | None = None,
    learner_note: str | None = None,
) -> dict[str, Any]:
    """§6.4's entry actions and §12.1's autosaved note.

    Three fields, and pointedly not a fourth: ``hypothesis`` is the
    Confusion-Tracker's and is not in this signature, so no request shape can
    reach it. §12.1 renders it with no input at all for the same reason.

    Every accepted change appends to ``journal_events``. The timeline is what
    §6.4 shows and what makes a resolved entry account for itself, so a status
    that moved without an event would leave a history that skips its own
    turning point.
    """
    row = session.execute(
        sql(
            """
            SELECT id, status FROM journal_entries
             WHERE id = :entry_id AND user_id = :user_id
             FOR UPDATE
            """
        ),
        {"entry_id": entry_id, "user_id": user_id},
    ).first()
    if row is None:
        raise NotFound(f"no journal entry {entry_id}")

    now = dt.datetime.now(dt.UTC)
    updates: list[str] = ["last_touched_at = :now"]
    params: dict[str, Any] = {"entry_id": entry_id, "now": now}

    if status is not None and status != row.status:
        updates.append("status = CAST(:status AS journal_status)")
        params["status"] = status
        # Cleared on any move away from resolved, so a reopened entry does not
        # keep claiming a resolution date it no longer has.
        updates.append("resolved_at = :resolved_at")
        params["resolved_at"] = now if status == "resolved" else None
        _add_event(session, entry_id, _STATUS_EVENT[status], now)

    if summary is not None:
        updates.append("summary = :summary")
        params["summary"] = summary

    if learner_note is not None:
        updates.append("learner_note = :learner_note")
        params["learner_note"] = learner_note
        _add_event(session, entry_id, "learner_note_added", now)

    session.execute(
        sql(  # noqa: S608 -- every fragment above is a literal
            f"UPDATE journal_entries SET {', '.join(updates)} WHERE id = :entry_id"
        ),
        params,
    )
    session.flush()
    return journal_entry(session, user_id, entry_id)


def _add_event(
    session: Session, entry_id: uuid.UUID, kind: str, at: dt.datetime
) -> None:
    session.execute(
        sql(
            """
            INSERT INTO journal_events (entry_id, kind, note, created_at)
            VALUES (:entry_id, CAST(:kind AS journal_event_kind), '', :at)
            """
        ),
        {"entry_id": entry_id, "kind": kind, "at": at},
    )


# --- session close (§6.5) --------------------------------------------------


def session_summary(
    session: Session, user_id: uuid.UUID, session_id: uuid.UUID
) -> dict[str, Any]:
    """The close modal's contents (§6.5).

    Raises :class:`NotFound` when the session has no summary row. That is not
    the same as "no such session": ``session_summaries`` exists precisely so
    that a row's presence means "this session is summarised" (data layer §6.11),
    and the close endpoint reports ``summarised: false`` when the Curator call
    failed. The client already has copy for that case; inventing an empty
    summary here would replace it with a panel that looks written and says
    nothing.
    """
    row = session.execute(
        sql(
            """
            SELECT ss.summary,
                   ss.open_threads,
                   ss.concepts_touched,
                   s.started_at,
                   s.ended_at,
                   s.total_cost_usd,
                   s.learner_subject_id
              FROM session_summaries ss
              JOIN learning_sessions s ON s.id = ss.session_id
             WHERE ss.session_id = :session_id AND s.user_id = :user_id
            """
        ),
        {"session_id": session_id, "user_id": user_id},
    ).first()
    if row is None:
        raise NotFound(f"no summary for session {session_id}")

    next_focus = session.execute(
        sql(
            """
            SELECT c.id, c.title
              FROM learner_subjects ls
              JOIN concepts c ON c.id = ls.current_focus_concept_id
             WHERE ls.id = :learner_subject_id
            """
        ),
        {"learner_subject_id": row.learner_subject_id},
    ).first()

    ended = row.ended_at or dt.datetime.now(dt.UTC)
    return {
        "session_id": str(session_id),
        "summary": row.summary,
        "concepts_touched": _mastery_deltas(
            session, session_id, list(row.concepts_touched or [])
        ),
        "open_threads": [str(t) for t in (row.open_threads or [])],
        "next_focus_concept_id": str(next_focus.id) if next_focus else None,
        "next_focus_concept_name": next_focus.title if next_focus else None,
        "duration_minutes": (ended - row.started_at).total_seconds() / 60.0,
        # The session's whole cost, not the summary call's. `total_cost_usd` is
        # trigger-maintained from `agent_traces` (data layer §6.6), which is
        # what a learner with cost display on is being shown.
        "cost_usd": float(row.total_cost_usd) if row.total_cost_usd is not None else None,
    }


def _mastery_deltas(
    session: Session, session_id: uuid.UUID, touched: list[uuid.UUID]
) -> list[dict[str, Any]]:
    """§6.5's "beta-reduction: 0.65 → 0.82".

    Read from ``mastery_events`` rather than from ``concept_mastery``: the
    current row holds one number and the delta needs two, and the event log is
    append-only and scoped by session, which is exactly the pair of endpoints
    this wants. First event's ``p_known_before``, last event's ``p_known_after``.

    Concepts the session touched without producing evidence are listed with a
    zero delta rather than omitted. §6.5's heading is "what we covered"; a
    lecture segment the learner never answered a question about was still
    covered, and dropping it would make the list quietly mean "what you were
    graded on".
    """
    rows = session.execute(
        sql(
            """
            SELECT cm.concept_id,
                   c.title AS concept_name,
                   (array_agg(me.p_known_before ORDER BY me.created_at))[1] AS before,
                   (array_agg(me.p_known_after ORDER BY me.created_at DESC))[1] AS after
              FROM mastery_events me
              JOIN concept_mastery cm ON cm.id = me.concept_mastery_id
              JOIN concepts c ON c.id = cm.concept_id
             WHERE me.session_id = :session_id
             GROUP BY cm.concept_id, c.title
             ORDER BY c.title
            """
        ),
        {"session_id": session_id},
    ).all()

    deltas = [
        {
            "concept_id": str(row.concept_id),
            "concept_name": row.concept_name,
            "before": float(row.before),
            "after": float(row.after),
        }
        for row in rows
    ]

    seen = {d["concept_id"] for d in deltas}
    remaining = [cid for cid in touched if str(cid) not in seen]
    if not remaining:
        return deltas

    unchanged = session.execute(
        sql(
            """
            SELECT c.id AS concept_id,
                   c.title AS concept_name,
                   COALESCE(
                       (SELECT cm.p_known
                          FROM concept_mastery cm
                          JOIN learning_sessions s
                            ON s.learner_subject_id = cm.learner_subject_id
                         WHERE cm.concept_id = c.id AND s.id = :session_id),
                       0.0
                   ) AS p_known
              FROM concepts c
             WHERE c.id = ANY(CAST(:ids AS uuid[]))
             ORDER BY c.title
            """
        ),
        {"session_id": session_id, "ids": remaining},
    ).all()

    deltas.extend(
        {
            "concept_id": str(row.concept_id),
            "concept_name": row.concept_name,
            "before": float(row.p_known),
            "after": float(row.p_known),
        }
        for row in unchanged
    )
    return deltas


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Decimal):  # pragma: no cover -- defensive
        return str(value)
    return str(value)


__all__ = [
    "NotFound",
    "desk",
    "journal_entries",
    "journal_entry",
    "patch_journal_entry",
    "session_summary",
]
