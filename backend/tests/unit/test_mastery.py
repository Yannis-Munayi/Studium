"""The BKT model and the forgetting curve (spec §3, §6.5)."""

from __future__ import annotations

import datetime as dt

import pytest

from studium.mastery import (
    DECAY_HALF_LIFE,
    DEFAULT_BKT,
    MASTERY_THRESHOLD,
    BKTParams,
    decayed,
    is_mastered,
    posterior,
)

NOW = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.UTC)


def test_correct_evidence_raises_the_posterior() -> None:
    p = BKTParams()
    assert posterior(0.5, correct=True, params=p) > 0.5


def test_incorrect_evidence_lowers_the_conditioned_estimate() -> None:
    """The transition term can offset a wrong answer, but the observation
    itself must always push the estimate down relative to a correct one."""
    p = BKTParams()
    assert posterior(0.5, correct=False, params=p) < posterior(0.5, correct=True, params=p)


def test_posterior_stays_in_range() -> None:
    p = BKTParams()
    for start in (0.0, 0.01, 0.5, 0.99, 1.0):
        for correct in (True, False):
            assert 0.0 <= posterior(start, correct, p) <= 1.0


def test_repeated_correct_answers_approach_certainty() -> None:
    p = BKTParams()
    value = 0.1
    for _ in range(12):
        value = posterior(value, correct=True, params=p)
    assert value > MASTERY_THRESHOLD


def test_slip_means_one_wrong_answer_does_not_erase_mastery() -> None:
    """A learner who knows the material can still slip. One wrong answer
    should dent the estimate, not reset it."""
    p = BKTParams()
    high = 0.95
    after = posterior(high, correct=False, params=p)
    assert 0.4 < after < high


def test_decay_is_identity_at_zero_age() -> None:
    assert decayed(0.9, NOW, now=NOW) == pytest.approx(0.9)


def test_decay_halves_at_the_half_life() -> None:
    then = NOW - DECAY_HALF_LIFE
    assert decayed(0.8, then, now=NOW) == pytest.approx(0.4, abs=1e-6)


def test_decay_with_no_evidence_is_the_raw_value() -> None:
    assert decayed(0.3, None, now=NOW) == pytest.approx(0.3)


def test_gating_uses_the_decayed_value() -> None:
    """The load-bearing case: a learner who mastered a concept two half-lives
    ago is no longer above threshold, so the concept re-locks."""
    long_ago = NOW - 2 * DECAY_HALF_LIFE
    assert is_mastered(0.95, NOW, now=NOW)
    assert not is_mastered(0.95, long_ago, now=NOW)


def test_bkt_params_round_trip() -> None:
    params = BKTParams(p_init=0.2, p_transit=0.3, p_slip=0.05, p_guess=0.15)
    assert BKTParams.from_jsonb(params.as_jsonb()) == params


def test_bkt_params_fill_defaults_from_partial_blob() -> None:
    params = BKTParams.from_jsonb({"p_slip": 0.42})
    assert params.p_slip == 0.42
    assert params.p_transit == DEFAULT_BKT["p_transit"]


def test_bkt_params_tolerates_empty_blob() -> None:
    """concept_mastery.bkt_params defaults to '{}' when a concept carries no
    metadata, so the empty case has to work."""
    assert BKTParams.from_jsonb({}) == BKTParams()
    assert BKTParams.from_jsonb(None) == BKTParams()
