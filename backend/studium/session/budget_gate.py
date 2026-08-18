"""Pre-flight budget enforcement (agent runtime §19, data layer §8).

"Cost caps are enforced at the runtime boundary ... This is the difference
between a project ended by a $2,000 surprise and a project bounded by a $100
hard cap."

The gate runs before any billable operation and refuses gracefully: the learner
is told, given the reset time, and their session is preserved. It never raises
past the Orchestrator.

**Estimates are deliberate overestimates.** §19's per-mode figures are
worst-case turn costs, so the gate errs toward blocking at session open rather
than mid-lecture. Blocking early is a mild disappointment; blocking a learner
halfway through a derivation is the thing worth paying a little conservatism to
avoid.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from studium.asyncdb import read_db
from studium.copy import degradation
from studium.cost import BudgetStatus, budget_status
from studium.session.context import BudgetWarning

log = logging.getLogger(__name__)

#: §19's per-mode worst-case turn cost estimates, in USD.
EXPECTED_MAX_USD: dict[str, float] = {
    "lecture": 0.25,
    "tutorial": 0.50,
    "lab": 0.30,
    "review": 0.15,
    "summative_assessment": 0.75,
    "office_hours": 0.50,
    "orientation": 0.30,
}
DEFAULT_EXPECTED_MAX_USD = 0.50

#: §16 body step 2: "Budget gate re-checked (cached; only revalidated every 5
#: turns unless approaching cap)." Re-reading five live cost sums on every turn
#: would put a database round trip in front of every model call for a number
#: that moves by cents.
REVALIDATE_EVERY_TURNS = 5

#: Within this fraction of the hard cap, revalidate on every turn regardless.
#: The cached path is only safe while there is room for several turns of drift.
APPROACHING_CAP_FRACTION = 0.85


class BudgetExceededError(RuntimeError):
    """A hard cap would be crossed by the operation about to run."""

    def __init__(
        self,
        *,
        scope: str,
        limit_usd: float,
        spent_usd: float,
        reset_at: dt.datetime,
        timezone: str = "America/Toronto",
    ) -> None:
        self.scope = scope
        self.limit_usd = limit_usd
        self.spent_usd = spent_usd
        self.reset_at = reset_at
        self.timezone = timezone
        super().__init__(
            f"{scope} cap ${limit_usd:.2f} would be exceeded (spent ${spent_usd:.2f})"
        )

    def learner_message(self) -> str:
        return degradation.budget_exceeded(
            scope=self.scope, reset_at=self.reset_at, timezone=self.timezone
        )


@dataclass(slots=True)
class GateResult:
    """Outcome of a gate check."""

    allowed: bool
    warnings: list[BudgetWarning]
    status: BudgetStatus


def expected_max_usd(mode: str) -> float:
    return EXPECTED_MAX_USD.get(mode, DEFAULT_EXPECTED_MAX_USD)


async def pre_flight_check(
    user_id: uuid.UUID,
    *,
    mode: str = "tutorial",
    timezone: str = "America/Toronto",
) -> GateResult:
    """Check both caps before a billable operation (§19).

    Raises :class:`BudgetExceededError` on a hard cap. Soft caps return as
    warnings the frontend renders -- §19 is explicit that a soft cap does not
    stop the session.
    """
    expected = expected_max_usd(mode)
    status = await read_db(lambda s: budget_status(s, user_id))

    if status.today_usd + expected > status.daily_hard_usd:
        raise BudgetExceededError(
            scope="daily",
            limit_usd=status.daily_hard_usd,
            spent_usd=status.today_usd,
            reset_at=degradation.start_of_tomorrow(timezone=timezone),
            timezone=timezone,
        )

    if status.month_usd + expected > status.monthly_hard_usd:
        raise BudgetExceededError(
            scope="monthly",
            limit_usd=status.monthly_hard_usd,
            spent_usd=status.month_usd,
            reset_at=degradation.start_of_next_month(timezone=timezone),
            timezone=timezone,
        )

    warnings: list[BudgetWarning] = []
    if status.today_usd + expected > status.daily_soft_usd:
        warnings.append(
            BudgetWarning(
                scope="daily",
                limit_usd=status.daily_soft_usd,
                spent_usd=status.today_usd,
            )
        )
    if status.month_usd + expected > status.monthly_soft_usd:
        warnings.append(
            BudgetWarning(
                scope="monthly",
                limit_usd=status.monthly_soft_usd,
                spent_usd=status.month_usd,
            )
        )

    return GateResult(allowed=True, warnings=warnings, status=status)


class BudgetCache:
    """Per-session cache of the gate result (§16).

    Revalidates every ``REVALIDATE_EVERY_TURNS`` turns, and on every turn once
    spend approaches the hard cap -- because the interval that is safe with
    $6 of headroom is not safe with 30 cents.
    """

    def __init__(self, *, every: int = REVALIDATE_EVERY_TURNS) -> None:
        self.every = every
        self._turns_since_check = 0
        self._last: GateResult | None = None

    def _approaching_cap(self) -> bool:
        if self._last is None:
            return True
        s = self._last.status
        daily = s.today_usd >= s.daily_hard_usd * APPROACHING_CAP_FRACTION
        monthly = s.month_usd >= s.monthly_hard_usd * APPROACHING_CAP_FRACTION
        return daily or monthly

    def needs_check(self) -> bool:
        return (
            self._last is None
            or self._turns_since_check >= self.every
            or self._approaching_cap()
        )

    async def check(
        self,
        user_id: uuid.UUID,
        *,
        mode: str = "tutorial",
        timezone: str = "America/Toronto",
    ) -> GateResult:
        # The counter advances *before* the test, so it means "turns since the
        # last read, including this one". Incrementing afterwards instead left
        # it one behind and revalidated on the sixth cached turn rather than
        # the fifth -- an extra turn of drift against the cap every cycle.
        if self._last is not None:
            self._turns_since_check += 1

        if not self.needs_check():
            assert self._last is not None
            return self._last

        self._last = await pre_flight_check(user_id, mode=mode, timezone=timezone)
        self._turns_since_check = 0
        return self._last

    def invalidate(self) -> None:
        """Force a fresh read on the next check."""
        self._last = None
        self._turns_since_check = self.every
