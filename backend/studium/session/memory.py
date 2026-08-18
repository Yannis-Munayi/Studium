"""Cross-session context assembly (agent runtime §6, §16).

Builds the :class:`SessionContext` every agent reads. This is the only place
the runtime queries the data layer for read-side state, which is what makes
"agents never query the database themselves" checkable rather than aspirational.

The context is assembled **once per turn**, not once per agent call. Several
agents can run inside one learner-facing exchange (the intent classifier, the
Curator, the Lecturer), and re-reading mastery between them would let two
agents in the same turn disagree about what the learner knows.

Decay is recomputed at read time rather than read from ``p_known_decayed``,
matching the data layer's own note (DIVERGENCES C1): the stored column is only
as fresh as the last decay job, and an agent reasoning from day-stale mastery
will re-teach what the learner knows or skip what they have forgotten.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.asyncdb import read_db, row_to_dict
from studium.llm.prompts import grounding_version
from studium.models import (
    Concept,
    LearnerSubject,
    LearningSession,
    Subject,
    User,
    UserProfile,
)
from studium.session.context import SessionContext

log = logging.getLogger(__name__)

#: §6: "last 12 turns of this session".
RECENT_TURN_LIMIT = 12

#: Open journal entries pulled into context. Beyond this the prompt is carrying
#: more history than it can act on in one turn.
JOURNAL_LIMIT = 20


async def assemble_context(
    session_id: uuid.UUID,
    *,
    exchange_index: int = 0,
) -> SessionContext:
    """Read everything an agent might need for one turn."""
    return await read_db(lambda s: _assemble(s, session_id, exchange_index))


def _assemble(
    session: Session, session_id: uuid.UUID, exchange_index: int
) -> SessionContext:
    row = session.execute(
        select(LearningSession, LearnerSubject, Subject, User, UserProfile)
        .join(LearnerSubject, LearnerSubject.id == LearningSession.learner_subject_id)
        .join(Subject, Subject.id == LearnerSubject.subject_id)
        .join(User, User.id == LearningSession.user_id)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .where(LearningSession.id == session_id)
    ).first()

    if row is None:
        raise LookupError(f"no learning session {session_id}")

    learning_session, enrollment, subject, user, profile = row

    learner = row_to_dict(user)
    learner["profile"] = row_to_dict(profile) if profile else {}

    focus_id = learning_session.focus_concept_id
    focus_concept = None
    if focus_id is not None:
        focus_row = session.get(Concept, focus_id)
        focus_concept = row_to_dict(focus_row) if focus_row else None

    subject_concepts = _subject_concepts(session, subject.id)
    mastery = _mastery_snapshot(session, enrollment.id)

    return SessionContext(
        session=row_to_dict(learning_session),
        learner=learner,
        learner_subject=row_to_dict(enrollment),
        subject=row_to_dict(subject),
        focus_concept=focus_concept,
        focus_neighborhood=_neighborhood(session, focus_id) if focus_id else [],
        subject_concepts=subject_concepts,
        recent_turns=_recent_turns(session, session_id),
        mastery_snapshot=mastery,
        open_journal_entries=_open_journal(session, enrollment.id),
        prior_session_summary=_prior_summary(session, enrollment.id, session_id),
        retrieval_check_result=_retrieval_check(session, session_id),
        concepts_seen=_concepts_seen(session, enrollment.id),
        grounding_version=_grounding(session, subject, focus_concept),
        exchange_index=exchange_index,
    )


def _subject_concepts(session: Session, subject_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every concept in the subject, with its prerequisite slugs.

    One query with an aggregate rather than N+1 per concept -- the Curator's
    prefix needs the whole graph and rebuilds it on a cache miss.
    """
    rows = session.execute(
        sql(
            """
            SELECT c.id, c.slug, c.title, c.depth, c.position,
                   c.is_load_bearing, c.estimated_minutes, c.metadata,
                   COALESCE(
                       array_agg(p.slug ORDER BY p.slug)
                           FILTER (WHERE p.slug IS NOT NULL),
                       '{}'
                   ) AS prerequisite_slugs
              FROM concepts c
              LEFT JOIN concept_edges e
                     ON e.to_concept_id = c.id AND e.kind = 'prerequisite'
              LEFT JOIN concepts p ON p.id = e.from_concept_id
             WHERE c.subject_id = :subject_id
             GROUP BY c.id
             ORDER BY c.position, c.slug
            """
        ),
        {"subject_id": subject_id},
    ).all()

    return [
        {
            "id": str(r.id),
            "slug": r.slug,
            "title": r.title,
            "depth": r.depth,
            "position": r.position,
            "is_load_bearing": r.is_load_bearing,
            "estimated_minutes": r.estimated_minutes,
            "metadata": r.metadata or {},
            "prerequisite_slugs": list(r.prerequisite_slugs or []),
        }
        for r in rows
    ]


def _mastery_snapshot(session: Session, learner_subject_id: uuid.UUID) -> dict[str, float]:
    """concept_id -> decayed mastery, computed in SQL from raw evidence."""
    from studium.mastery import DECAY_HALF_LIFE

    rows = session.execute(
        sql(
            """
            SELECT concept_id,
                   CASE
                     WHEN last_evidence_at IS NULL THEN p_known
                     ELSE p_known
                          * pow(0.5, EXTRACT(EPOCH FROM (NOW() - last_evidence_at))
                                     / :half_life_seconds)
                   END AS decayed
              FROM concept_mastery
             WHERE learner_subject_id = :learner_subject_id
            """
        ),
        {
            "learner_subject_id": learner_subject_id,
            "half_life_seconds": DECAY_HALF_LIFE.total_seconds(),
        },
    ).all()
    return {str(r.concept_id): float(r.decayed) for r in rows}


def _neighborhood(session: Session, concept_id: uuid.UUID) -> list[dict[str, Any]]:
    """Prerequisites plus immediate dependents of the focus concept (§6)."""
    rows = session.execute(
        sql(
            """
            SELECT DISTINCT c.id, c.slug, c.title, c.depth, c.is_load_bearing,
                            c.metadata
              FROM concept_edges e
              JOIN concepts c
                ON c.id = CASE
                            WHEN e.to_concept_id = :concept_id
                              THEN e.from_concept_id
                            ELSE e.to_concept_id
                          END
             WHERE (e.to_concept_id = :concept_id OR e.from_concept_id = :concept_id)
               AND e.kind IN ('prerequisite', 'dependency')
             ORDER BY c.depth, c.slug
            """
        ),
        {"concept_id": concept_id},
    ).all()
    return [
        {
            "id": str(r.id),
            "slug": r.slug,
            "title": r.title,
            "depth": r.depth,
            "is_load_bearing": r.is_load_bearing,
            "metadata": r.metadata or {},
        }
        for r in rows
    ]


def _recent_turns(session: Session, session_id: uuid.UUID) -> list[dict[str, Any]]:
    """The last N turns, oldest first.

    Fetched newest-first (so the index serves the LIMIT) then reversed, because
    a prompt reads chronologically.
    """
    rows = session.execute(
        sql(
            """
            SELECT turn_index, actor, input, output, primitive, concept_id, created_at
              FROM session_turns
             WHERE session_id = :session_id
             ORDER BY turn_index DESC
             LIMIT :limit
            """
        ),
        {"session_id": session_id, "limit": RECENT_TURN_LIMIT},
    ).all()

    return [
        {
            "turn_index": r.turn_index,
            "actor": r.actor,
            "input": r.input or {},
            "output": r.output or {},
            "primitive": r.primitive,
            "concept_id": str(r.concept_id) if r.concept_id else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in reversed(rows)
    ]


def _open_journal(session: Session, learner_subject_id: uuid.UUID) -> list[dict[str, Any]]:
    """Open and partial entries, with their concept slug for prompt legibility.

    ``hypothesis`` is included: these entries feed the Tutor and the
    Confusion-Tracker, which are exactly the readers §6.7 intends for it. The
    ``studium.acl.project_journal_entry`` projection strips it on any path that
    reaches the learner.
    """
    rows = session.execute(
        sql(
            """
            SELECT je.id, je.concept_id, c.slug AS concept_slug, je.status,
                   je.summary, je.hypothesis, je.origin, je.first_seen_at,
                   je.last_touched_at
              FROM journal_entries je
              LEFT JOIN concepts c ON c.id = je.concept_id
             WHERE je.learner_subject_id = :learner_subject_id
               AND je.status IN ('open', 'partial')
             ORDER BY je.last_touched_at DESC
             LIMIT :limit
            """
        ),
        {"learner_subject_id": learner_subject_id, "limit": JOURNAL_LIMIT},
    ).all()

    return [
        {
            "id": str(r.id),
            "concept_id": str(r.concept_id) if r.concept_id else None,
            "concept_slug": r.concept_slug,
            "status": r.status,
            "summary": r.summary,
            "hypothesis": r.hypothesis,
            "origin": r.origin,
            "first_seen_at": r.first_seen_at.isoformat() if r.first_seen_at else None,
            "last_touched_at": r.last_touched_at.isoformat() if r.last_touched_at else None,
        }
        for r in rows
    ]


def _prior_summary(
    session: Session, learner_subject_id: uuid.UUID, current_session_id: uuid.UUID
) -> dict[str, Any] | None:
    """The most recent *other* session's summary (§16 step 4).

    Excludes the current session: at close time the current session has a
    summary too, and feeding it back as "last time" would have the Curator open
    the next session against itself.
    """
    row = session.execute(
        sql(
            """
            SELECT ss.session_id, ss.summary, ss.key_points, ss.open_threads,
                   ss.concepts_touched, ls.ended_at, ls.mode
              FROM session_summaries ss
              JOIN learning_sessions ls ON ls.id = ss.session_id
             WHERE ls.learner_subject_id = :learner_subject_id
               AND ls.id <> :current_session_id
               AND ls.ended_at IS NOT NULL
             ORDER BY ls.ended_at DESC
             LIMIT 1
            """
        ),
        {
            "learner_subject_id": learner_subject_id,
            "current_session_id": current_session_id,
        },
    ).first()

    if row is None:
        return None
    return {
        "session_id": str(row.session_id),
        "summary": row.summary,
        "key_points": row.key_points or [],
        "open_threads": row.open_threads or [],
        "concepts_touched": [str(c) for c in (row.concepts_touched or [])],
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "mode": row.mode,
    }


def _retrieval_check(session: Session, session_id: uuid.UUID) -> dict[str, Any] | None:
    row = session.execute(
        sql(
            """
            SELECT prompts, responses, scores, overall_score
              FROM retrieval_checks
             WHERE session_id = :session_id
             ORDER BY created_at DESC
             LIMIT 1
            """
        ),
        {"session_id": session_id},
    ).first()

    if row is None:
        return None
    scores = row.scores or []
    prompts = row.prompts or []
    weak = [
        prompts[i].get("prompt", "")
        for i, s in enumerate(scores)
        if i < len(prompts) and isinstance(s, dict) and s.get("verdict") != "correct"
    ]
    return {
        "prompts": prompts,
        "responses": row.responses or [],
        "scores": scores,
        "overall_score": float(row.overall_score),
        "weak_prompts": weak,
    }


def _concepts_seen(session: Session, learner_subject_id: uuid.UUID) -> list[str]:
    """Slugs the learner has met before, so the Lecturer does not re-teach them."""
    rows = session.execute(
        sql(
            """
            SELECT DISTINCT c.slug
              FROM concept_mastery cm
              JOIN concepts c ON c.id = cm.concept_id
             WHERE cm.learner_subject_id = :learner_subject_id
               AND cm.evidence_count > 0
             ORDER BY c.slug
            """
        ),
        {"learner_subject_id": learner_subject_id},
    ).all()
    return [r.slug for r in rows]


def _grounding(
    session: Session, subject: Subject, focus_concept: dict[str, Any] | None
) -> str:
    """§17's ``grounding_version``, computed once per context assembly."""
    if focus_concept is None:
        return grounding_version(
            subject_updated_at=subject.updated_at, concept_updated_at=None
        )

    max_chunk = session.execute(
        sql(
            """
            SELECT max(sc.created_at)
              FROM concept_sources cs
              CROSS JOIN LATERAL unnest(cs.chunk_ids) AS ref(chunk_id)
              JOIN source_chunks sc ON sc.id = ref.chunk_id
             WHERE cs.concept_id = :concept_id
            """
        ),
        {"concept_id": focus_concept["id"]},
    ).scalar()

    return grounding_version(
        subject_updated_at=subject.updated_at,
        concept_updated_at=focus_concept.get("updated_at"),
        max_chunk_updated_at=max_chunk,
    )
