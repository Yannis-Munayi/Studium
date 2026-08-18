"""Budget enforcement thresholds (agent runtime §23 Tier 1, §19).

§23: "Budget enforcement: ``pre_flight_check`` raises ``BudgetExceededError``
at the correct thresholds."

The gate is patched at the ``budget_status`` boundary rather than mocked at the
database, so the arithmetic under test is the real arithmetic: spend plus the
per-mode worst-case estimate against each of the four caps.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from studium.copy import degradation
from studium.cost import BudgetStatus
from studium.session import budget_gate
from studium.session.budget_gate import (
    APPROACHING_CAP_FRACTION,
    EXPECTED_MAX_USD,
    BudgetCache,
    BudgetExceededError,
    expected_max_usd,
    pre_flight_check,
)

USER = uuid.UUID("00000000-0000-7000-8000-00000000a001")


def status(**overrides) -> BudgetStatus:
    base = {
        "today_usd": 0.0,
        "month_usd": 0.0,
        "daily_soft_usd": 5.0,
        "daily_hard_usd": 8.0,
        "monthly_soft_usd": 75.0,
        "monthly_hard_usd": 100.0,
    }
    base.update(overrides)
    return BudgetStatus(**base)


@pytest.fixture
def patched(monkeypatch):
    """Swap the live cost read for a fixed status."""

    def install(s: BudgetStatus) -> list[int]:
        calls: list[int] = []

        async def fake_read_db(fn):
            calls.append(1)
            return s

        monkeypatch.setattr(budget_gate, "read_db", fake_read_db)
        return calls

    return install


class TestHardCaps:
    async def test_allows_a_turn_with_headroom(self, patched):
        patched(status(today_usd=1.0, month_usd=20.0))
        result = await pre_flight_check(USER, mode="tutorial")
        assert result.allowed is True
        assert result.warnings == []

    async def test_blocks_when_the_estimate_would_cross_the_daily_hard_cap(self, patched):
        """§19 blocks *before* the operation, on spend + worst-case estimate."""
        # 7.60 spent + 0.50 tutorial estimate = 8.10 > 8.00 hard cap.
        patched(status(today_usd=7.60))
        with pytest.raises(BudgetExceededError) as exc:
            await pre_flight_check(USER, mode="tutorial")
        assert exc.value.scope == "daily"

    async def test_does_not_block_when_the_estimate_just_fits(self, patched):
        """7.40 + 0.50 = 7.90, under 8.00. The boundary is not off by one."""
        patched(status(today_usd=7.40))
        assert (await pre_flight_check(USER, mode="tutorial")).allowed is True

    async def test_a_cheaper_mode_is_allowed_where_an_expensive_one_is_not(self, patched):
        """The estimate is per-mode (§19), not a flat figure."""
        patched(status(today_usd=7.60))
        # review estimates $0.15: 7.75 < 8.00.
        assert (await pre_flight_check(USER, mode="review")).allowed is True
        with pytest.raises(BudgetExceededError):
            await pre_flight_check(USER, mode="summative_assessment")

    async def test_blocks_on_the_monthly_hard_cap(self, patched):
        patched(status(today_usd=0.5, month_usd=99.8))
        with pytest.raises(BudgetExceededError) as exc:
            await pre_flight_check(USER, mode="lecture")
        assert exc.value.scope == "monthly"

    async def test_daily_is_checked_before_monthly(self, patched):
        """Both blown: report the one that resets sooner, so the advice is useful."""
        patched(status(today_usd=99.0, month_usd=999.0))
        with pytest.raises(BudgetExceededError) as exc:
            await pre_flight_check(USER, mode="lecture")
        assert exc.value.scope == "daily"


class TestSoftCaps:
    async def test_soft_cap_warns_without_blocking(self, patched):
        """§19: a soft cap is 'not raised; passed as a warning'."""
        patched(status(today_usd=4.9))
        result = await pre_flight_check(USER, mode="tutorial")
        assert result.allowed is True
        assert [w.scope for w in result.warnings] == ["daily"]

    async def test_both_soft_caps_can_warn_at_once(self, patched):
        patched(status(today_usd=4.9, month_usd=74.9))
        warnings = (await pre_flight_check(USER, mode="tutorial")).warnings
        assert {w.scope for w in warnings} == {"daily", "monthly"}

    async def test_warning_carries_the_numbers_the_learner_needs(self, patched):
        patched(status(today_usd=4.9))
        warning = (await pre_flight_check(USER, mode="tutorial")).warnings[0]
        assert warning.spent_usd == 4.9
        assert warning.limit_usd == 5.0
        assert "$4.90" in warning.message()


class TestEstimates:
    def test_every_session_mode_has_an_estimate(self):
        """A mode with no estimate would silently use the generic default."""
        from studium.models.base import session_mode

        for mode in session_mode.enums:
            assert mode in EXPECTED_MAX_USD, mode

    def test_estimates_match_the_section_19_figures(self):
        assert EXPECTED_MAX_USD["lecture"] == 0.25
        assert EXPECTED_MAX_USD["tutorial"] == 0.50
        assert EXPECTED_MAX_USD["lab"] == 0.30
        assert EXPECTED_MAX_USD["review"] == 0.15
        assert EXPECTED_MAX_USD["summative_assessment"] == 0.75

    def test_an_unknown_mode_falls_back_rather_than_raising(self):
        assert expected_max_usd("interpretive_dance") == 0.50


class TestCaching:
    async def test_revalidates_only_every_n_turns_with_headroom(self, patched):
        """§16: 'cached; only revalidated every 5 turns unless approaching cap'."""
        calls = patched(status(today_usd=0.5, month_usd=5.0))
        cache = BudgetCache(every=5)

        for _ in range(5):
            await cache.check(USER, mode="tutorial")
        assert len(calls) == 1

        await cache.check(USER, mode="tutorial")
        assert len(calls) == 2

    async def test_revalidates_every_turn_when_approaching_the_cap(self, patched):
        """The interval that is safe with $6 of headroom is not safe with 30 cents."""
        calls = patched(status(today_usd=8.0 * APPROACHING_CAP_FRACTION + 0.1))
        cache = BudgetCache(every=5)

        for _ in range(3):
            await cache.check(USER, mode="review")
        assert len(calls) == 3

    async def test_invalidate_forces_a_fresh_read(self, patched):
        calls = patched(status(today_usd=0.5))
        cache = BudgetCache(every=5)
        await cache.check(USER, mode="tutorial")
        cache.invalidate()
        await cache.check(USER, mode="tutorial")
        assert len(calls) == 2


class TestLearnerFacingCopy:
    def test_the_error_carries_copy_not_a_stack_trace(self):
        """§21: the learner sees a coherent response, never a raw error."""
        exc = BudgetExceededError(
            scope="daily",
            limit_usd=8.0,
            spent_usd=8.1,
            reset_at=dt.datetime(2026, 8, 18, 0, 0, tzinfo=dt.UTC),
        )
        message = exc.learner_message()
        assert "progress is saved" in message.lower()
        assert "daily" in message
        assert "Error" not in message and "Exception" not in message

    def test_reset_time_is_rendered_in_the_learners_timezone(self):
        """A UTC timestamp is accurate and useless to the learner."""
        midnight_utc = dt.datetime(2026, 8, 18, 4, 0, tzinfo=dt.UTC)
        toronto = degradation.budget_exceeded(
            scope="daily", reset_at=midnight_utc, timezone="America/Toronto"
        )
        tokyo = degradation.budget_exceeded(
            scope="daily", reset_at=midnight_utc, timezone="Asia/Tokyo"
        )
        assert toronto != tokyo

    def test_an_unknown_timezone_still_produces_a_message(self):
        """A bad tz string must not turn a budget notice into a 500."""
        message = degradation.budget_exceeded(
            scope="daily",
            reset_at=dt.datetime(2026, 8, 18, 4, 0, tzinfo=dt.UTC),
            timezone="Mars/Olympus_Mons",
        )
        assert "usage limit" in message

    def test_reset_helpers_land_on_midnight(self):
        tomorrow = degradation.start_of_tomorrow(timezone="America/Toronto")
        assert (tomorrow.hour, tomorrow.minute, tomorrow.second) == (0, 0, 0)

        next_month = degradation.start_of_next_month(timezone="America/Toronto")
        assert next_month.day == 1
