"""The reviewer's cost report (infrastructure §13).

§3: "Cost is measured, not estimated. ... Projections in this spec are for
planning; the numbers that matter are the ones the running system produces."

Two surfaces, deliberately not joined:

* **LLM cost** (§13.1) comes from ``cost_ledger``, which data layer §6.12 rolls
  up daily per user per model across five categories. Everything in this module
  reads that.
* **Infrastructure cost** (§13.2) comes from provider invoices and is not in
  any database. :data:`INFRASTRUCTURE_ESTIMATE` records §13.2's planning
  figures so ``cost-report`` can print them *labelled as estimates* beside the
  measured numbers. Merging them into one total would produce a figure that is
  two-thirds measured and one-third guess, presented as one number.

**The report reads the ledger, and the ledger is a day behind by design.**
``cost_rollup.roll_up_day`` runs per completed day; today's spend lives in the
source tables until it does. ``studium.cost.today_spent`` is the live sum and
is what the budget gate reads. A report over a window ending today would
therefore under-report today, so :func:`report` says so in the output rather
than silently adding an inconsistent figure.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

#: §13.1's anomaly rule: "any user whose spend is > 3x their trailing 7-day
#: average".
ANOMALY_MULTIPLE = 3.0

#: §13.2's monthly figures. Planning numbers from the spec, reproduced here so
#: the report can show them next to the measured LLM cost and be explicit that
#: only one of the two columns is real.
INFRASTRUCTURE_ESTIMATE: dict[str, tuple[float, float, str]] = {
    "fly.io": (50.0, 100.0, "backend + frontend + Postgres + volumes (§4)"),
    "cloudflare r2": (0.0, 0.0, "not provisioned until §14.3's 30GB trigger"),
    "langfuse": (0.0, 0.0, "free tier; ~$50 if extended retention is bought"),
    "sentry": (0.0, 0.0, "free tier; ~$25 at classroom scale"),
}

#: The five ledger columns, in the order §13.1 lists the cost lines.
COST_LINES: tuple[tuple[str, str], ...] = (
    ("agent", "cost_agent_usd"),
    ("content", "cost_content_usd"),
    ("ingestion", "cost_ingestion_usd"),
    ("summary", "cost_summary_usd"),
    ("grading", "cost_grading_usd"),
)


@dataclass(slots=True)
class UserSpend:
    user_id: str | None
    email: str
    total_usd: float
    by_line: dict[str, float] = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.user_id is None:
            return "(erased learners, de-identified)"
        return self.email or self.user_id


@dataclass(slots=True)
class Anomaly:
    email: str
    day: dt.date
    spend_usd: float
    trailing_average_usd: float

    @property
    def multiple(self) -> float:
        return self.spend_usd / self.trailing_average_usd if self.trailing_average_usd else 0.0


@dataclass(slots=True)
class CostReport:
    start: dt.date
    end: dt.date
    by_user: list[UserSpend]
    by_line: dict[str, float]
    daily: list[tuple[dt.date, float]]
    anomalies: list[Anomaly]
    #: True when ``end`` includes today, whose rows the roll-up has not written.
    includes_today: bool
    unattributed_content: tuple[int, int]

    @property
    def total_usd(self) -> float:
        return sum(self.by_line.values())

    def projection_usd(self, today: dt.date | None = None) -> float:
        """§13.1's "projection to end of billing period", straight-line.

        Only meaningful when the window starts at a month boundary, which is
        what ``cost-report`` defaults to. Returns 0.0 otherwise rather than
        extrapolating an arbitrary window to a month, which would be a number
        with no referent.
        """
        today = today or dt.date.today()
        if self.start != self.start.replace(day=1):
            return 0.0
        import calendar

        days_elapsed = max(1, min(self.end, today).day)
        days_in_month = calendar.monthrange(self.start.year, self.start.month)[1]
        return (self.total_usd / days_elapsed) * days_in_month

    def render(self) -> str:
        lines = [
            f"LLM cost, {self.start} to {self.end}  (data layer §6.12's ledger)",
            "",
            f"  total  ${self.total_usd:,.2f}",
        ]
        if self.includes_today:
            lines.append(
                "  NOTE: the window includes today, whose rows the daily "
                "roll-up has not written yet. Today reads as $0 here; "
                "studium.cost.today_spent is the live figure the budget gate uses."
            )

        lines += ["", "  by cost line"]
        for name, _ in COST_LINES:
            lines.append(f"    {name:12} ${self.by_line.get(name, 0.0):>10,.2f}")

        lines += ["", "  by user"]
        for spend in self.by_user:
            lines.append(f"    {spend.label:44} ${spend.total_usd:>10,.2f}")
        if not self.by_user:
            lines.append("    (no ledger rows in this window)")

        projection = self.projection_usd()
        if projection:
            lines += [
                "",
                f"  projected to end of month  ${projection:,.2f}  "
                f"(straight-line; §13.1)",
            ]

        lines += ["", "  daily"]
        for day, amount in self.daily:
            bar = "#" * min(40, int(amount * 4))
            lines.append(f"    {day}  ${amount:>8,.2f}  {bar}")

        lines += ["", f"  anomalies (> {ANOMALY_MULTIPLE:g}x trailing 7-day average)"]
        if self.anomalies:
            for anomaly in self.anomalies:
                lines.append(
                    f"    {anomaly.day}  {anomaly.email:36} "
                    f"${anomaly.spend_usd:,.2f} vs ${anomaly.trailing_average_usd:,.2f} "
                    f"({anomaly.multiple:.1f}x)"
                )
        else:
            lines.append("    none")

        orphan, total = self.unattributed_content
        lines += [
            "",
            "  attribution health (SPEC_DEBT SD2)",
            f"    {orphan} of {total} costed content artifacts in this window "
            f"had no triggering session and booked to the system account.",
        ]
        if total and orphan / total > 0.2:
            lines.append(
                "    ^ high. Check the content pipeline still sets "
                "content_artifacts.generated_for_session_id; while it does not, "
                "the per-user figures above understate real learner spend."
            )

        lines += ["", "  infrastructure (§13.2 -- ESTIMATES, not measured)"]
        low = sum(v[0] for v in INFRASTRUCTURE_ESTIMATE.values())
        high = sum(v[1] for v in INFRASTRUCTURE_ESTIMATE.values())
        for provider, (lo, hi, note) in INFRASTRUCTURE_ESTIMATE.items():
            lines.append(f"    {provider:16} ${lo:>6,.0f}-${hi:<6,.0f}  {note}")
        lines.append(f"    {'total':16} ${low:>6,.0f}-${high:<6,.0f}  per month")
        lines.append(
            "    Reconcile against the actual invoices monthly (§13.2). These "
            "numbers come from the spec, not from a provider."
        )
        return "\n".join(lines)


def report(session: Session, *, start: dt.date, end: dt.date) -> CostReport:
    """§13.1's report over a closed date range, inclusive."""
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    window = {"start": start, "end": end}

    line_sums = {
        name: float(
            session.execute(
                sql(
                    f"SELECT COALESCE(SUM({column}), 0) FROM cost_ledger "
                    f" WHERE day >= :start AND day <= :end"
                ),
                window,
            ).scalar_one()
        )
        for name, column in COST_LINES
    }

    user_rows = session.execute(
        sql(
            """
            SELECT cl.user_id,
                   COALESCE(u.email, '') AS email,
                   SUM(cl.cost_usd)           AS total,
                   SUM(cl.cost_agent_usd)     AS agent,
                   SUM(cl.cost_content_usd)   AS content,
                   SUM(cl.cost_ingestion_usd) AS ingestion,
                   SUM(cl.cost_summary_usd)   AS summary,
                   SUM(cl.cost_grading_usd)   AS grading
              FROM cost_ledger cl
              LEFT JOIN users u ON u.id = cl.user_id
             WHERE cl.day >= :start AND cl.day <= :end
             GROUP BY cl.user_id, u.email
             ORDER BY total DESC
            """
        ),
        window,
    ).all()

    by_user = [
        UserSpend(
            user_id=str(r.user_id) if r.user_id else None,
            email=r.email,
            total_usd=float(r.total or 0),
            by_line={
                "agent": float(r.agent or 0),
                "content": float(r.content or 0),
                "ingestion": float(r.ingestion or 0),
                "summary": float(r.summary or 0),
                "grading": float(r.grading or 0),
            },
        )
        for r in user_rows
    ]

    daily = [
        (row.day, float(row.total or 0))
        for row in session.execute(
            sql(
                """
                SELECT day, SUM(cost_usd) AS total
                  FROM cost_ledger
                 WHERE day >= :start AND day <= :end
                 GROUP BY day ORDER BY day
                """
            ),
            window,
        )
    ]

    orphan_row = session.execute(
        sql(
            """
            SELECT count(*) FILTER (WHERE generated_for_session_id IS NULL) AS orphan,
                   count(*) AS total
              FROM content_artifacts
             WHERE cost_usd IS NOT NULL
               AND generated_at >= CAST(:start AS date)
               AND generated_at < CAST(:end AS date) + INTERVAL '1 day'
            """
        ),
        window,
    ).one()

    return CostReport(
        start=start,
        end=end,
        by_user=by_user,
        by_line=line_sums,
        daily=daily,
        anomalies=anomalies(session, start=start, end=end),
        includes_today=end >= dt.date.today(),
        unattributed_content=(int(orphan_row.orphan or 0), int(orphan_row.total or 0)),
    )


def anomalies(session: Session, *, start: dt.date, end: dt.date) -> list[Anomaly]:
    """§13.1: "any user whose spend is > 3x their trailing 7-day average".

    The trailing average excludes the day being tested -- otherwise a spike
    inflates its own baseline by a seventh and the rule fires later than it
    should. Days with no trailing history at all are skipped rather than
    treated as an infinite multiple: a learner's first day is not an anomaly.
    """
    rows = session.execute(
        sql(
            """
            WITH daily AS (
                SELECT cl.user_id, cl.day, SUM(cl.cost_usd) AS spend
                  FROM cost_ledger cl
                 WHERE cl.user_id IS NOT NULL
                   AND cl.day >= CAST(:start AS date) - INTERVAL '7 days'
                   AND cl.day <= :end
                 GROUP BY cl.user_id, cl.day
            ),
            windowed AS (
                SELECT user_id, day, spend,
                       AVG(spend) OVER (
                           PARTITION BY user_id ORDER BY day
                           ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING
                       ) AS trailing,
                       COUNT(*) OVER (
                           PARTITION BY user_id ORDER BY day
                           ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING
                       ) AS history
                  FROM daily
            )
            SELECT COALESCE(u.email, w.user_id::text) AS email,
                   w.day, w.spend, w.trailing
              FROM windowed w
              LEFT JOIN users u ON u.id = w.user_id
             WHERE w.day >= :start
               AND w.history > 0
               AND w.trailing > 0
               AND w.spend > w.trailing * :multiple
             ORDER BY w.spend DESC
            """
        ),
        {"start": start, "end": end, "multiple": ANOMALY_MULTIPLE},
    ).all()
    return [
        Anomaly(
            email=r.email,
            day=r.day,
            spend_usd=float(r.spend),
            trailing_average_usd=float(r.trailing),
        )
        for r in rows
    ]
