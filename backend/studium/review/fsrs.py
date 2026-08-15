"""FSRS scheduling (spec §6.9).

The schema does not enforce the state machine: the reviewer service is trusted
to compute ``stability_after``, ``difficulty_after`` and ``due_at``, and the
append-only event log makes miscalculations recoverable -- you can replay
``review_events`` under corrected parameters.

Parameter defaults are the published FSRS-4.5 weights, which §16 calls
"defensible for a general-purpose deployment". Per-learner tuning is a v1.1
feature; nothing here assumes the weights are global forever.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, replace
from enum import IntEnum

#: FSRS-4.5 default weights.
DEFAULT_WEIGHTS: tuple[float, ...] = (
    0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.031,
    1.6474, 0.1367, 1.0461, 2.1072, 0.0793, 0.3246, 1.587, 0.2272,
    2.8755,
)

#: Target retention at review time. 0.9 is the FSRS default.
DESIRED_RETENTION = 0.9

#: FSRS forgetting-curve constants.
_FACTOR = 19.0 / 81.0
_DECAY = -0.5

MIN_STABILITY = 0.1
MIN_DIFFICULTY = 1.0
MAX_DIFFICULTY = 10.0
MAX_INTERVAL_DAYS = 365 * 10


class Rating(IntEnum):
    AGAIN = 1
    HARD = 2
    GOOD = 3
    EASY = 4


class State:
    NEW = "new"
    LEARNING = "learning"
    REVIEW = "review"
    RELEARNING = "relearning"


@dataclass(frozen=True, slots=True)
class CardState:
    """The subset of ``review_cards`` FSRS reads and writes."""

    stability: float = 1.0
    difficulty: float = 5.0
    retrievability: float = 1.0
    reps: int = 0
    lapses: int = 0
    state: str = State.NEW
    last_reviewed_at: dt.datetime | None = None
    due_at: dt.datetime | None = None


def retrievability(stability: float, elapsed_days: float) -> float:
    """Probability of recall after ``elapsed_days`` at the given stability."""
    if stability <= 0:
        return 0.0
    return float((1.0 + _FACTOR * elapsed_days / stability) ** _DECAY)


def interval_for(stability: float, retention: float = DESIRED_RETENTION) -> float:
    """Days until recall probability falls to ``retention``."""
    return (stability / _FACTOR) * (retention ** (1.0 / _DECAY) - 1.0)


def review(
    card: CardState,
    rating: Rating | int,
    *,
    now: dt.datetime | None = None,
    weights: tuple[float, ...] = DEFAULT_WEIGHTS,
    retention: float = DESIRED_RETENTION,
) -> CardState:
    """Apply one review and return the next card state.

    Pure: the caller persists the result and writes the ``review_events`` row
    with the before/after pairs, so the transition is always auditable.
    """
    rating = Rating(int(rating))
    now = now or dt.datetime.now(dt.UTC)
    w = weights

    if card.state == State.NEW or card.last_reviewed_at is None:
        stability = _initial_stability(rating, w)
        difficulty = _initial_difficulty(rating, w)
        recall = 1.0
    else:
        elapsed = max(0.0, (now - card.last_reviewed_at).total_seconds() / 86400.0)
        recall = retrievability(card.stability, elapsed)
        difficulty = _next_difficulty(card.difficulty, rating, w)
        if rating is Rating.AGAIN:
            stability = _forget_stability(card, recall, w)
        else:
            stability = _recall_stability(card, recall, rating, w)

    stability = max(MIN_STABILITY, stability)
    difficulty = _clamp(difficulty, MIN_DIFFICULTY, MAX_DIFFICULTY)

    if rating is Rating.AGAIN:
        state = State.RELEARNING
        lapses = card.lapses + 1
        due = now + dt.timedelta(minutes=10)
    else:
        state = State.REVIEW
        lapses = card.lapses
        days = _clamp(round(interval_for(stability, retention)), 1, MAX_INTERVAL_DAYS)
        due = now + dt.timedelta(days=days)

    return replace(
        card,
        stability=stability,
        difficulty=difficulty,
        retrievability=recall,
        reps=card.reps + 1,
        lapses=lapses,
        state=state,
        last_reviewed_at=now,
        due_at=due,
    )


def _initial_stability(rating: Rating, w: tuple[float, ...]) -> float:
    return max(MIN_STABILITY, w[rating - 1])


def _initial_difficulty(rating: Rating, w: tuple[float, ...]) -> float:
    return _clamp(w[4] - math.exp(w[5] * (rating - 1)) + 1.0, MIN_DIFFICULTY, MAX_DIFFICULTY)


def _next_difficulty(difficulty: float, rating: Rating, w: tuple[float, ...]) -> float:
    delta = -w[6] * (rating - 3)
    updated = difficulty + delta * (10.0 - difficulty) / 9.0
    # Mean reversion toward the "easy" anchor keeps difficulty from ratcheting.
    anchor = _initial_difficulty(Rating.EASY, w)
    return w[7] * anchor + (1.0 - w[7]) * updated


def _recall_stability(
    card: CardState, recall: float, rating: Rating, w: tuple[float, ...]
) -> float:
    hard_penalty = w[15] if rating is Rating.HARD else 1.0
    easy_bonus = w[16] if rating is Rating.EASY else 1.0
    growth = (
        math.exp(w[8])
        * (11.0 - card.difficulty)
        * math.pow(card.stability, -w[9])
        * (math.exp(w[10] * (1.0 - recall)) - 1.0)
        * hard_penalty
        * easy_bonus
    )
    return card.stability * (1.0 + growth)


def _forget_stability(card: CardState, recall: float, w: tuple[float, ...]) -> float:
    return (
        w[11]
        * math.pow(card.difficulty, -w[12])
        * (math.pow(card.stability + 1.0, w[13]) - 1.0)
        * math.exp(w[14] * (1.0 - recall))
    )


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))
