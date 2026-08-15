"""FSRS scheduling behaviour (spec §6.9).

The schema trusts this module to compute stability, difficulty and due dates,
so the properties that matter are tested rather than the exact numbers -- the
weights are tunable and §16 defers them.
"""

from __future__ import annotations

import datetime as dt

import pytest

from studium.review.fsrs import (
    CardState,
    Rating,
    State,
    interval_for,
    retrievability,
    review,
)

NOW = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.UTC)


def test_new_card_becomes_review_on_a_good_answer() -> None:
    card = review(CardState(), Rating.GOOD, now=NOW)
    assert card.state == State.REVIEW
    assert card.reps == 1
    assert card.lapses == 0
    assert card.due_at > NOW


def test_again_sends_a_card_to_relearning_and_counts_a_lapse() -> None:
    card = review(CardState(), Rating.GOOD, now=NOW)
    lapsed = review(card, Rating.AGAIN, now=NOW + dt.timedelta(days=5))
    assert lapsed.state == State.RELEARNING
    assert lapsed.lapses == 1
    assert lapsed.stability < card.stability


def test_easy_schedules_further_out_than_hard() -> None:
    base = review(CardState(), Rating.GOOD, now=NOW)
    later = NOW + dt.timedelta(days=10)
    easy = review(base, Rating.EASY, now=later)
    hard = review(base, Rating.HARD, now=later)
    assert easy.due_at > hard.due_at


def test_repeated_good_answers_lengthen_the_interval() -> None:
    card = CardState()
    now = NOW
    intervals: list[float] = []
    for _ in range(5):
        card = review(card, Rating.GOOD, now=now)
        assert card.due_at is not None
        intervals.append((card.due_at - now).total_seconds())
        now = card.due_at
    assert intervals == sorted(intervals), "intervals should be non-decreasing"
    assert intervals[-1] > intervals[0]


def test_difficulty_stays_within_the_fsrs_scale() -> None:
    """review_cards has a CHECK on 1..10; the scheduler must never violate it."""
    card = CardState()
    now = NOW
    for rating in [Rating.AGAIN] * 10 + [Rating.EASY] * 10:
        card = review(card, rating, now=now)
        assert 1.0 <= card.difficulty <= 10.0
        assert card.stability > 0
        assert 0.0 <= card.retrievability <= 1.0
        now = card.due_at or now


def test_retrievability_decays_with_time() -> None:
    assert retrievability(10.0, 0.0) == pytest.approx(1.0)
    assert retrievability(10.0, 10.0) < 1.0
    assert retrievability(10.0, 100.0) < retrievability(10.0, 10.0)


def test_higher_stability_means_a_longer_interval() -> None:
    assert interval_for(20.0) > interval_for(10.0)


def test_review_is_pure() -> None:
    """The event log records before/after pairs, so the input state must not
    be mutated in place."""
    card = CardState()
    review(card, Rating.GOOD, now=NOW)
    assert card == CardState()


def test_rating_accepts_a_raw_int() -> None:
    """review_events.rating is a SMALLINT read straight from the row."""
    assert review(CardState(), 3, now=NOW).state == State.REVIEW
