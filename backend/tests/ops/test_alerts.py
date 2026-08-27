"""Alert configuration (infrastructure §8, §16 Tier 1 line 3).

The tests that matter here are structural rather than numerical. Every
threshold in §8.1 is an educated first guess §1 expects production to sharpen,
so asserting on the numbers would pin guesses. What is worth pinning is the
shape: that §8.1's table is complete, that no condition is watched by nothing,
that §8.2's list and §8.1's do not overlap, and that a condition with no
recommended action does not survive.

The last one is the interesting one. **A gate whose input is absent must fail,
not pass** -- evaluation's ``coverage_gaps`` in a different costume. A
threshold written down and watched by nothing reads as coverage on a page and
provides none, and the only way to notice is a test that refuses it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from studium.ops import alerts

#: §8.1's rows. Listed here rather than derived from CONDITIONS, so a row
#: deleted from the code fails rather than shrinking the expectation with it.
SPEC_CONDITIONS = {
    "uptime_failure",
    "sentry_error_rate_spike",
    "budget_exceeded_error",
    "missing_provenance",
    "content_review_queue_depth",
    "ingestion_review_queue_depth",
    "postgres_storage",
    "vm_memory",
    "anthropic_5xx_rate",
    "voyage_5xx_rate",
    "evaluation_regression",
    "cost_trending_to_cap",
}


def test_every_spec_condition_is_configured() -> None:
    """§8.1's twelve rows all exist."""
    missing = SPEC_CONDITIONS - set(alerts.BY_KEY)
    assert not missing, f"§8.1 names conditions that are not configured: {sorted(missing)}"


def test_additions_beyond_the_spec_are_deliberate() -> None:
    """Anything not in §8.1 is an addition, and each closes a named gap.

    Asserted so an addition cannot arrive unexamined: §3 warns that confusing
    "actionable" with "changing" produces alert fatigue, and the cheapest way
    to get there is by adding conditions one at a time without anyone counting.
    """
    additions = set(alerts.BY_KEY) - SPEC_CONDITIONS
    assert additions == {"unattributed_content", "retention_worker_stale"}, (
        f"unexpected additions to §8.1: {sorted(additions)}. Each addition "
        f"needs a stated reason -- see SPEC_DEBT SD2 and §12.1."
    )


@pytest.mark.parametrize("condition", alerts.CONDITIONS, ids=lambda c: c.key)
def test_no_condition_is_watched_by_nothing(condition: alerts.Condition) -> None:
    """The failure this whole module exists to prevent.

    A condition with no probe and no named external home is a threshold nobody
    is checking, written down in a way that reads as coverage.
    """
    assert condition.probe is not None or condition.configured_in.strip(), (
        f"{condition.key} has no probe and names no system that watches it. "
        f"Either give it a probe or say where it is configured; a threshold "
        f"nobody measures is worse than no threshold, because the silence is "
        f"read as evidence."
    )


@pytest.mark.parametrize("condition", alerts.CONDITIONS, ids=lambda c: c.key)
def test_every_condition_says_what_to_do(condition: alerts.Condition) -> None:
    """§8: alerts exist "for conditions where the reviewer needs to do something".

    A condition with no action either has nothing to do -- in which case it is
    a dashboard metric, not an alert -- or has something to do that nobody
    wrote down, which is the same thing at 03:00.
    """
    assert condition.action.strip(), f"{condition.key} states no action"


@pytest.mark.parametrize("condition", alerts.CONDITIONS, ids=lambda c: c.key)
def test_every_condition_has_a_threshold_and_a_response_window(
    condition: alerts.Condition,
) -> None:
    assert condition.threshold.strip()
    assert condition.delivery.strip()
    assert condition.response_within in {
        alerts.RESPONSE_IMMEDIATE,
        alerts.RESPONSE_1H,
        alerts.RESPONSE_4H,
        alerts.RESPONSE_24H,
        alerts.RESPONSE_48H,
        alerts.RESPONSE_1W,
    }


def test_alerting_and_not_alerting_are_disjoint() -> None:
    """§8.1 and §8.2 cannot both claim a condition.

    The list that decays is §8.2's: a condition quietly migrating from "queue
    item" to "alert" is how the inbox stops being read.
    """
    both = set(alerts.BY_KEY) & {key for key, _ in alerts.NOT_ALERTED}
    assert not both, f"listed as both alerting and not alerting: {sorted(both)}"


def test_not_alerted_entries_carry_their_reasoning() -> None:
    for key, reason in alerts.NOT_ALERTED:
        assert reason.strip(), f"{key} is excluded from alerting with no reason given"


def test_thresholds_are_numbers() -> None:
    """§8.1 states them as prose; tuning one should be a reviewable diff."""
    for key, value in alerts.THRESHOLDS.items():
        assert isinstance(value, int | float), f"{key} is not a number"
        assert value > 0, f"{key} is {value}; a non-positive threshold fires always or never"


def test_fractional_thresholds_are_fractions_not_percentages() -> None:
    """The unit confusion that would silently disable three conditions.

    ``postgres_storage_fraction = 80`` rather than ``0.80`` compares a ratio
    against 80 and never fires, and nothing about the reading says so.
    """
    for key in (
        "postgres_storage_fraction",
        "vm_memory_fraction",
        "provider_5xx_rate",
        "month_end_cost_fraction_of_cap",
        "unattributed_content_ratio",
    ):
        assert 0 < alerts.THRESHOLDS[key] <= 1, (
            f"{key} is {alerts.THRESHOLDS[key]}; fractions are 0-1, and a "
            f"percentage here would make the condition unreachable"
        )


def test_probes_and_external_conditions_are_both_present() -> None:
    """Neither category is empty.

    All-external would mean `check-alerts` verifies nothing; all-probe would
    mean §8.1's Sentry and uptime rows had been quietly dropped.
    """
    probed = [c for c in alerts.CONDITIONS if c.probe is not None]
    external = [c for c in alerts.CONDITIONS if c.externally_watched]
    assert probed and external


# --- the cost-trend arithmetic, which has real edge cases -------------------


def test_days_in_month_handles_february() -> None:
    assert alerts._days_in_month(dt.date(2026, 2, 10)) == 28
    assert alerts._days_in_month(dt.date(2028, 2, 10)) == 29
    assert alerts._days_in_month(dt.date(2026, 12, 1)) == 31


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value

    def one(self) -> object:
        return self._value


class _FakeSession:
    """Returns queued answers in order. Enough for the arithmetic tests.

    A real database would exercise the SQL and not the extrapolation; the SQL
    is covered in Tier 2. What is worth isolating here is the day-1 guard,
    which needs a specific date and would be awkward to seed.
    """

    def __init__(self, *answers: object) -> None:
        self._answers = list(answers)

    def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
        return _FakeResult(self._answers.pop(0))


class _Row:
    def __init__(self, spent: float, cap: float) -> None:
        self.spent = spent
        self.cap = cap


def test_cost_trend_never_fires_on_the_first_of_the_month() -> None:
    """$2 by 09:00 on the 1st projects to $60 and means nothing.

    §13.3's response window is a week, so one day of delay costs nothing and
    the false positive would cost the alert its credibility.
    """
    session = _FakeSession(_Row(spent=2.0, cap=0.0), 8.0)
    measurement = alerts._cost_trend(session, today=dt.date(2026, 8, 1))
    assert not measurement.firing
    assert "day 1 never fires" in measurement.detail


def test_cost_trend_fires_when_the_projection_crosses_the_cap() -> None:
    # $60 over 15 days of a 31-day month projects to $124 against a $100 cap.
    session = _FakeSession(_Row(spent=60.0, cap=0.0), 100.0)
    measurement = alerts._cost_trend(session, today=dt.date(2026, 8, 15))
    assert measurement.firing
    assert measurement.context["projected_usd"] == pytest.approx(124.0)


def test_cost_trend_does_not_divide_by_a_zero_cap() -> None:
    """No caps configured is a misconfiguration, not an alert.

    Firing here would report "spend is infinitely over budget" for a database
    whose real problem is an empty user_budget_caps table -- a diagnosis
    pointing away from the cause.
    """
    session = _FakeSession(_Row(spent=500.0, cap=0.0), 0.0)
    measurement = alerts._cost_trend(session, today=dt.date(2026, 8, 15))
    assert not measurement.firing
    assert measurement.value == 0.0
