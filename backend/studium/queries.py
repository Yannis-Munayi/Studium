"""The frequent access paths from spec v1.1 §8.

These double as correctness tests for the schema: if a common access path is
awkward here, the schema is wrong. The query-shape tests in §14 snapshot
EXPLAIN output for each of them.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Row, select, text
from sqlalchemy.orm import Session

# Budget checking lives in studium.cost, which v1.1 §8 names as its home
# ("A helper studium.cost.today_spent(user_id) centralizes this logic so no
# caller assembles it by hand"). Re-exported so call sites need not care.
from .cost import BudgetStatus, budget_status, today_spent
from .models import (
    Concept,
    ContentArtifact,
    JournalEntry,
    JournalEvent,
    LearnerSubject,
    LearningSession,
    ReviewCard,
    SessionSummary,
    Subject,
    SubjectMetadata,
    User,
    UserProfile,
)

# --- session start ---------------------------------------------------------
#
# Five queries, issued in parallel from the request handler. Together they run
# in single-digit milliseconds on a warm cache at MVP scale.


def learner_with_profile(session: Session, user_id: uuid.UUID) -> Row[Any] | None:
    return session.execute(
        select(User, UserProfile.preferences, UserProfile.accessibility)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .where(User.id == user_id)
        .where(User.deleted_at.is_(None))
    ).first()


def active_enrollments(session: Session, user_id: uuid.UUID) -> list[Row[Any]]:
    return list(
        session.execute(
            select(
                LearnerSubject,
                Subject.title,
                Subject.slug,
                SubjectMetadata.concept_count,
                SubjectMetadata.load_bearing_count,
            )
            .join(Subject, Subject.id == LearnerSubject.subject_id)
            .outerjoin(SubjectMetadata, SubjectMetadata.subject_id == Subject.id)
            .where(LearnerSubject.user_id == user_id)
            .where(LearnerSubject.archived_at.is_(None))
        ).all()
    )


def open_journal_entries(
    session: Session, learner_subject_id: uuid.UUID, *, limit: int = 20
) -> list[JournalEntry]:
    return list(
        session.execute(
            select(JournalEntry)
            .where(JournalEntry.learner_subject_id == learner_subject_id)
            .where(JournalEntry.status.in_(("open", "partial")))
            .order_by(JournalEntry.last_touched_at.desc())
            .limit(limit)
        ).scalars()
    )


def due_review_cards(
    session: Session, learner_subject_id: uuid.UUID, *, limit: int = 10
) -> list[Row[Any]]:
    return list(
        session.execute(
            select(ReviewCard, Concept.title, Concept.slug)
            .join(Concept, Concept.id == ReviewCard.concept_id)
            .where(ReviewCard.learner_subject_id == learner_subject_id)
            .where(ReviewCard.due_at <= text("NOW()"))
            .where(ReviewCard.suspended.is_(False))
            .order_by(ReviewCard.due_at)
            .limit(limit)
        ).all()
    )


def prior_session_summary(
    session: Session, learner_subject_id: uuid.UUID
) -> Row[Any] | None:
    """Feeds retrieval-check generation at the start of the next session."""
    return session.execute(
        select(SessionSummary, LearningSession.mode, LearningSession.focus_concept_id)
        .join(LearningSession, LearningSession.id == SessionSummary.session_id)
        .where(LearningSession.learner_subject_id == learner_subject_id)
        .where(LearningSession.ended_at.is_not(None))
        .order_by(LearningSession.ended_at.desc())
        .limit(1)
    ).first()


# --- content ---------------------------------------------------------------


def lecture_segments(
    session: Session, concept_id: uuid.UUID, stance: str
) -> list[ContentArtifact]:
    """Current lecture segments for a concept, in order.

    The spec's version LEFT JOINs citations in the same statement, which
    multiplies each artifact by its citation count with no grouping. Citations
    are fetched separately by ``citations_for`` instead.
    """
    return list(
        session.execute(
            select(ContentArtifact)
            .where(ContentArtifact.concept_id == concept_id)
            .where(ContentArtifact.kind == "lecture_segment")
            .where(ContentArtifact.stance == stance)
            .where(ContentArtifact.status == "active")
            .where(ContentArtifact.retired_at.is_(None))
            .where(ContentArtifact.deleted_at.is_(None))
            .order_by(text("(content_artifacts.metadata ->> 'segment_index')::int"))
        ).scalars()
    )


def citations_for(session: Session, artifact_ids: list[uuid.UUID]) -> list[Row[Any]]:
    """Grounding for a set of artifacts: chunk text plus source attribution."""
    if not artifact_ids:
        return []
    sql = text(
        """
        SELECT cc.artifact_id,
               cc.source_chunk_id,
               cc.quoted_span,
               sc.text  AS source_text,
               sc.page_start,
               s.title  AS source_title,
               s.authors
          FROM content_citations cc
          JOIN source_chunks sc ON sc.id = cc.source_chunk_id
          JOIN sources s        ON s.id = sc.source_id
         WHERE cc.artifact_id = ANY(CAST(:ids AS uuid[]))
         ORDER BY cc.artifact_id, sc.chunk_index
        """
    )
    return list(session.execute(sql, {"ids": artifact_ids}).all())


# --- journal ---------------------------------------------------------------


def open_journal_entry(
    session: Session,
    *,
    user_id: uuid.UUID,
    learner_subject_id: uuid.UUID,
    summary: str,
    hypothesis: str = "",
    concept_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    origin: str = "tracker_inferred",
    note: str = "",
) -> JournalEntry:
    """Create an entry and its opening event in one transaction."""
    entry = JournalEntry(
        user_id=user_id,
        learner_subject_id=learner_subject_id,
        concept_id=concept_id,
        first_session_id=session_id,
        summary=summary,
        hypothesis=hypothesis,
        origin=origin,
    )
    session.add(entry)
    session.flush()  # assign the id
    session.add(
        JournalEvent(
            entry_id=entry.id, session_id=session_id, kind="created", note=note
        )
    )
    return entry


# --- session turns ---------------------------------------------------------


def next_turn_index(session: Session, session_id: uuid.UUID) -> int:
    """Allocate the next turn index inside the caller's transaction.

    The spec assigns these from an in-memory per-session counter, relying on
    "session affinity in the load balancer" for uniqueness -- but affinity pins
    a machine, not a process, so two workers on one machine collide. Deriving
    it in-transaction costs one indexed lookup. See DIVERGENCES.md (C7).

    Serialised by locking the parent ``learning_sessions`` row rather than the
    turns: Postgres rejects ``FOR UPDATE`` alongside an aggregate, and locking
    the rows you are counting would not block a concurrent inserter anyway --
    there is no row yet for the index about to be taken.
    """
    session.execute(
        text("SELECT id FROM learning_sessions WHERE id = :session_id FOR UPDATE"),
        {"session_id": session_id},
    ).one()

    return int(
        session.execute(
            text(
                """
                SELECT COALESCE(MAX(turn_index), -1) + 1
                  FROM session_turns
                 WHERE session_id = :session_id
                """
            ),
            {"session_id": session_id},
        ).scalar_one()
    )


__all__ = [
    "BudgetStatus",
    "active_enrollments",
    "budget_status",
    "citations_for",
    "due_review_cards",
    "learner_with_profile",
    "lecture_segments",
    "next_turn_index",
    "open_journal_entries",
    "open_journal_entry",
    "prior_session_summary",
    "today_spent",
]
