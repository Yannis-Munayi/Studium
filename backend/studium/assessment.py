"""Assessment scoring and the pass decision (spec §3, §6.8).

The grading agent produces per-criterion grades. It does not decide whether
the learner passed -- that is ``score >= threshold``, computed here.

The spec defines ``grade IN (0, 1, 2)`` and ``rubric_criteria.weight IN
(1, 2, 3)`` but never states how they combine into the attempt's 0..1 score,
which ``compute_pass`` needs. The rule below is the obvious weighted mean;
it is written down here because it is load-bearing and was previously only
implicit. See DIVERGENCES.md (C14).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AssessmentAttempt, AssessmentResponse, RubricCriterion

#: Highest per-criterion grade. 0 = missing, 1 = partial, 2 = complete.
MAX_GRADE = 2

#: Fallback pass mark, matching the existing draft. Per-subject overrides live
#: on ``subjects.assessment_threshold`` and are snapshotted onto the attempt.
DEFAULT_THRESHOLD = 0.75


@dataclass(frozen=True, slots=True)
class GradedCriterion:
    grade: int
    weight: int


def score_attempt(graded: Sequence[GradedCriterion]) -> float:
    """Weighted mean of per-criterion grades, normalised to 0..1.

    An attempt with no graded criteria scores 0 rather than raising: a
    submitted-but-empty attempt is a legitimate state, and it should fail.
    """
    total_weight = sum(g.weight for g in graded)
    if total_weight == 0:
        return 0.0
    earned = sum(g.grade * g.weight for g in graded)
    return earned / (total_weight * MAX_GRADE)


def compute_pass(score: float | None, threshold: float) -> bool | None:
    """The pass decision. NULL score means not yet graded, so NULL result.

    This is the only place a pass is decided. Nothing calls a model here.
    """
    if score is None:
        return None
    return score >= threshold


def grade_attempt(session: Session, attempt_id: uuid.UUID) -> AssessmentAttempt:
    """Score every graded response on an attempt and record the result.

    Ungraded responses (``grade IS NULL``) are excluded from the weighted mean
    rather than counted as zero -- an attempt is only fully scored once the
    Evaluator has visited every criterion, which the caller checks via
    ``is_fully_graded``.
    """
    attempt = session.execute(
        select(AssessmentAttempt).where(AssessmentAttempt.id == attempt_id)
    ).scalar_one()

    rows = session.execute(
        select(AssessmentResponse.grade, RubricCriterion.weight)
        .join(
            RubricCriterion,
            RubricCriterion.id == AssessmentResponse.rubric_criterion_id,
        )
        .where(AssessmentResponse.attempt_id == attempt_id)
    ).all()

    graded = [
        GradedCriterion(grade=int(g), weight=int(w)) for g, w in rows if g is not None
    ]

    attempt.score = score_attempt(graded)
    attempt.passed = compute_pass(attempt.score, float(attempt.threshold))
    return attempt


def is_fully_graded(session: Session, attempt_id: uuid.UUID) -> bool:
    pending = session.execute(
        select(AssessmentResponse.id)
        .where(AssessmentResponse.attempt_id == attempt_id)
        .where(AssessmentResponse.grade.is_(None))
        .limit(1)
    ).first()
    return pending is None
