"""Due-card selection (agent runtime §14).

Deterministic and model-free. §14: "The Reviewer LLM is not involved in
selection -- it is a database query." Reads ``review_cards`` where the card is
due and not suspended, weights load-bearing concepts up, and caps at the target
count.

The weighting is the only judgement here, and it is deliberately crude: a
load-bearing concept is worth reviewing before a leaf concept that came due the
same day, because the leaf depends on it. Anything more elaborate belongs in
the Evaluation spec's remit, where it can be measured against retention rather
than asserted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

#: §14 sizing: a review block inside a 90-minute session is a handful of cards,
#: not a deck. Callers override for a dedicated review session.
DEFAULT_TARGET_CARDS = 8

#: How far a load-bearing concept jumps the queue, in days of due-date credit.
#: One day: enough to order two cards due the same day, not enough to pull a
#: load-bearing card ahead of one that has been overdue for a week.
LOAD_BEARING_PRIORITY_DAYS = 1.0


@dataclass(frozen=True, slots=True)
class DueCard:
    """One card selected for review."""

    id: uuid.UUID
    concept_id: uuid.UUID
    concept_title: str
    concept_slug: str
    stability: float
    difficulty: float
    retrievability: float
    reps: int
    lapses: int
    state: str
    due_at: Any
    last_reviewed_at: Any
    is_load_bearing: bool

    def as_payload(self) -> dict[str, Any]:
        """Shape the Reviewer's ``card`` payload expects."""
        return {
            "id": str(self.id),
            "concept_id": str(self.concept_id),
            "concept_title": self.concept_title,
            "stability": self.stability,
            "difficulty": self.difficulty,
            "retrievability": self.retrievability,
            "reps": self.reps,
            "lapses": self.lapses,
            "state": self.state,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "last_reviewed_at": (
                self.last_reviewed_at.isoformat() if self.last_reviewed_at else None
            ),
        }


def select_cards(
    session: Session,
    learner_subject_id: uuid.UUID,
    *,
    target: int = DEFAULT_TARGET_CARDS,
) -> list[DueCard]:
    """Cards due now, most overdue first, load-bearing concepts weighted up.

    Uses the ``idx_review_cards_due`` partial index (data layer §6.9), which is
    built on ``(learner_subject_id, due_at) WHERE NOT suspended`` -- exactly
    this predicate.
    """
    rows = session.execute(
        sql(
            """
            SELECT rc.id,
                   rc.concept_id,
                   c.title            AS concept_title,
                   c.slug             AS concept_slug,
                   rc.stability,
                   rc.difficulty,
                   rc.retrievability,
                   rc.reps,
                   rc.lapses,
                   rc.state,
                   rc.due_at,
                   rc.last_reviewed_at,
                   c.is_load_bearing
              FROM review_cards rc
              JOIN concepts c ON c.id = rc.concept_id
             WHERE rc.learner_subject_id = :learner_subject_id
               AND rc.due_at <= NOW()
               AND NOT rc.suspended
             ORDER BY rc.due_at
                      - CASE WHEN c.is_load_bearing
                             THEN make_interval(days => :priority_days)
                             ELSE make_interval(days => 0)
                        END
             LIMIT :target
            """
        ),
        {
            "learner_subject_id": learner_subject_id,
            "target": target,
            "priority_days": LOAD_BEARING_PRIORITY_DAYS,
        },
    ).all()

    return [
        DueCard(
            id=r.id,
            concept_id=r.concept_id,
            concept_title=r.concept_title,
            concept_slug=r.concept_slug,
            stability=float(r.stability),
            difficulty=float(r.difficulty),
            retrievability=float(r.retrievability),
            reps=int(r.reps),
            lapses=int(r.lapses),
            state=str(r.state),
            due_at=r.due_at,
            last_reviewed_at=r.last_reviewed_at,
            is_load_bearing=bool(r.is_load_bearing),
        )
        for r in rows
    ]


def due_count(session: Session, learner_subject_id: uuid.UUID) -> int:
    """How many cards are waiting. Feeds the Curator's pacing decision."""
    return int(
        session.execute(
            sql(
                """
                SELECT count(*)
                  FROM review_cards
                 WHERE learner_subject_id = :learner_subject_id
                   AND due_at <= NOW()
                   AND NOT suspended
                """
            ),
            {"learner_subject_id": learner_subject_id},
        ).scalar_one()
    )
