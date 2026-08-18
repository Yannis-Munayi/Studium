"""Session context, cross-session memory, and the budget gate (§6, §16, §19).

``lifecycle`` is deliberately **not** re-exported here. It composes agents
(the Curator writes the summary and picks the next focus), so importing it from
this package's ``__init__`` would make ``studium.session`` depend on
``studium.agents``, which depends back on ``studium.session.context`` for the
object every agent reads -- a cycle.

The layering that avoids it: ``context`` is a leaf, ``memory`` and
``budget_gate`` read the data layer, and ``lifecycle`` sits above the agents.
Import it directly (``from studium.session.lifecycle import close_session``).
"""

from .budget_gate import BudgetCache, BudgetExceededError, pre_flight_check
from .context import BudgetWarning, Passage, SessionContext
from .memory import assemble_context

__all__ = [
    "BudgetCache",
    "BudgetExceededError",
    "BudgetWarning",
    "Passage",
    "SessionContext",
    "assemble_context",
    "pre_flight_check",
]
