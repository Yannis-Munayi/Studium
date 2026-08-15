"""Live spend and budget enforcement (spec v1.1 §8, §6.12).

The ledger is rolled up daily, so a budget check that read only the ledger
would let a single long session run all day unbilled. ``today_spent`` closes
that window by summing the day's not-yet-rolled-up rows across all five cost
sources.

§8 asks for exactly this and names the helper: "A helper
``studium.cost.today_spent(user_id)`` centralizes this logic so no caller
assembles it by hand."
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from .models.identity import SYSTEM_USER_ID

#: One row per cost source: the column holding the money, the timestamp that
#: decides which day it lands on, and the join that attributes it to a user.
#: Kept as data rather than five hand-written queries so the roll-up in
#: studium.jobs.cost_rollup and this live sum cannot drift apart.
LIVE_COST_SOURCES: tuple[tuple[str, str], ...] = (
    (
        "agent",
        """
        SELECT COALESCE(SUM(t.cost_usd), 0)
          FROM agent_traces t
         WHERE t.user_id = :user_id
           AND t.created_at >= date_trunc('day', NOW())
        """,
    ),
    (
        "content",
        """
        SELECT COALESCE(SUM(ca.cost_usd), 0)
          FROM content_artifacts ca
          LEFT JOIN learning_sessions ls ON ls.id = ca.generated_for_session_id
         WHERE ca.cost_usd IS NOT NULL
           AND ca.generated_at >= date_trunc('day', NOW())
           AND COALESCE(ls.user_id, :system_user_id) = :user_id
        """,
    ),
    (
        "ingestion",
        """
        SELECT COALESCE(SUM(j.cost_usd), 0)
          FROM ingestion_jobs j
          JOIN sources s ON s.id = j.source_id
         WHERE j.finished_at >= date_trunc('day', NOW())
           AND COALESCE(s.uploaded_by, :system_user_id) = :user_id
        """,
    ),
    (
        "summary",
        """
        SELECT COALESCE(SUM(ss.cost_usd), 0)
          FROM session_summaries ss
          JOIN learning_sessions ls ON ls.id = ss.session_id
         WHERE ss.generated_at >= date_trunc('day', NOW())
           AND ls.user_id = :user_id
        """,
    ),
    (
        "grading",
        """
        SELECT COALESCE(SUM(a.grading_cost_usd), 0)
          FROM assessment_attempts a
         WHERE a.grading_cost_usd IS NOT NULL
           AND a.graded_at >= date_trunc('day', NOW())
           AND a.user_id = :user_id
        """,
    ),
)


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """What a caller needs to decide whether to start a billable operation."""

    today_usd: float
    month_usd: float
    daily_soft_usd: float
    daily_hard_usd: float
    monthly_soft_usd: float
    monthly_hard_usd: float

    @property
    def blocked(self) -> bool:
        """Hard cap reached: no new LLM calls until the window rolls over."""
        return (
            self.today_usd >= self.daily_hard_usd
            or self.month_usd >= self.monthly_hard_usd
        )

    @property
    def warn(self) -> bool:
        """Soft cap reached: warn the learner and keep going (§6.12)."""
        return (
            self.today_usd >= self.daily_soft_usd
            or self.month_usd >= self.monthly_soft_usd
        )


def today_spent(session: Session, user_id: uuid.UUID) -> dict[str, float]:
    """The day's cost per category, including rows the roll-up has not seen.

    Returns one entry per source plus ``total``.
    """
    params = {"user_id": user_id, "system_user_id": SYSTEM_USER_ID}
    spent = {
        name: float(session.execute(text(sql), params).scalar_one())
        for name, sql in LIVE_COST_SOURCES
    }
    spent["total"] = sum(spent.values())
    return spent


def budget_status(session: Session, user_id: uuid.UUID) -> BudgetStatus:
    """Called before any billable operation.

    Sums the rolled-up ledger and adds today's live spend on top. The ledger
    rows for today may be partially written -- the job is idempotent and
    re-runs -- so today's ledger contribution is deliberately excluded and
    taken from the live sources instead, which is the only figure guaranteed
    current.
    """
    sql = text(
        """
        WITH caps AS (
            SELECT daily_soft_usd, daily_hard_usd,
                   monthly_soft_usd, monthly_hard_usd
              FROM user_budget_caps
             WHERE user_id = :user_id
        ),
        ledger AS (
            SELECT COALESCE(SUM(cost_usd) FILTER (
                       WHERE day >= date_trunc('month', CURRENT_DATE)::date
                         AND day < CURRENT_DATE
                   ), 0) AS month_before_today
              FROM cost_ledger
             WHERE user_id = :user_id
        )
        SELECT ledger.month_before_today,
               caps.daily_soft_usd, caps.daily_hard_usd,
               caps.monthly_soft_usd, caps.monthly_hard_usd
          FROM caps, ledger
        """
    )
    row = session.execute(sql, {"user_id": user_id}).one_or_none()
    if row is None:
        raise LookupError(f"no budget caps configured for user {user_id}")

    today = today_spent(session, user_id)["total"]
    return BudgetStatus(
        today_usd=today,
        month_usd=float(row.month_before_today) + today,
        daily_soft_usd=float(row.daily_soft_usd),
        daily_hard_usd=float(row.daily_hard_usd),
        monthly_soft_usd=float(row.monthly_soft_usd),
        monthly_hard_usd=float(row.monthly_hard_usd),
    )
