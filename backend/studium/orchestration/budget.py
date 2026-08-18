"""Cost cap enforcement at the orchestration boundary (agent runtime §5, §19).

The spec's directory layout names ``orchestration/budget.py`` for cost cap
enforcement and ``session/budget_gate.py`` for the pre-flight check. Those are
one mechanism, and implementing it twice would give the runtime two places to
disagree about whether a learner is over their cap.

The implementation lives in :mod:`studium.session.budget_gate`, next to the
session lifecycle that consumes it. This module is the orchestration-side name
for it. See DIVERGENCES (R11).
"""

from __future__ import annotations

from studium.session.budget_gate import (
    APPROACHING_CAP_FRACTION,
    EXPECTED_MAX_USD,
    REVALIDATE_EVERY_TURNS,
    BudgetCache,
    BudgetExceededError,
    GateResult,
    expected_max_usd,
    pre_flight_check,
)

__all__ = [
    "APPROACHING_CAP_FRACTION",
    "EXPECTED_MAX_USD",
    "REVALIDATE_EVERY_TURNS",
    "BudgetCache",
    "BudgetExceededError",
    "GateResult",
    "expected_max_usd",
    "pre_flight_check",
]
