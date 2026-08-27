"""Alert conditions and their evaluation (infrastructure §8).

§3: "Alerts fire on what's actionable, not what's changing. ... Confusing the
two produces alert fatigue and hides the alerts that matter." §8.1's table is
reproduced here as data, and §8.2's list of things that deliberately do *not*
alert is reproduced beside it -- because the second list is the one that decays.
A condition that quietly migrates from "queue item" to "alert" is how the inbox
stops being read, and a Tier 1 test asserts no condition appears in both.

**Two kinds of condition, and only one of them can be checked from here.**

*Database-sourced* conditions (queue depth, storage headroom, cost trend) carry
a ``probe``: a function of a session returning the measured value. ``evaluate``
runs them and returns what fired. These are the conditions ``studium ops
check-alerts`` can answer for itself, which is what makes them testable against
a seeded database rather than against a screenshot of a dashboard.

*Externally-sourced* conditions (Sentry error rate, uptime pings, provider 5xx
rates, VM memory) carry ``configured_in`` naming the system that watches them
and the setting that has to exist there. They have no probe, and the Tier 1
test asserts precisely that: a condition with neither a probe nor a named
external home is a threshold nobody is checking, written down in a way that
reads as coverage. That failure mode is evaluation's ``coverage_gaps`` in a
different costume -- **a gate whose input is absent must fail, not pass.**

**Thresholds are numbers here, not prose.** §8.1 states them in a table cell;
``THRESHOLDS`` holds them as values the probes compare against, so tuning one
is a diff rather than an edit to a dashboard nobody can review.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

#: §8.1's thresholds, as numbers. Every one of these is an educated first guess
#: that §1 expects production to sharpen; keeping them in one block is what
#: makes "what did we change and when" answerable from the git history.
THRESHOLDS: dict[str, float] = {
    "uptime_consecutive_failures": 2,
    "sentry_error_rate_multiple": 10,
    "review_queue_depth": 20,
    "ingestion_queue_depth": 20,
    "postgres_storage_fraction": 0.80,
    "vm_memory_fraction": 0.90,
    "provider_5xx_rate": 0.05,
    "month_end_cost_fraction_of_cap": 0.90,
    # Not in §8.1. See UNATTRIBUTED_CONTENT_RATIO below and SPEC_DEBT (SD2).
    "unattributed_content_ratio": 0.20,
}

#: §4.4's volume, which the storage condition measures against.
POSTGRES_VOLUME_GB = 40

#: Severity in the sense §8.1's "response time expected" column means: how long
#: the reviewer has, given §8.3's email-to-one-person delivery.
RESPONSE_IMMEDIATE = "immediate"
RESPONSE_1H = "within 1 hour"
RESPONSE_4H = "within 4 hours"
RESPONSE_24H = "within 24 hours"
RESPONSE_48H = "within 48 hours"
RESPONSE_1W = "within 1 week"


@dataclass(frozen=True, slots=True)
class Condition:
    """One row of §8.1."""

    key: str
    description: str
    #: §8.1's threshold, as prose, for the alert body a human reads.
    threshold: str
    delivery: str
    response_within: str
    #: Measures the condition. None for conditions watched by Sentry, the
    #: uptime monitor or Fly, which this process cannot see.
    probe: Callable[[Session], Measurement] | None = None
    #: Where an externally-watched condition is configured. Required when
    #: ``probe`` is None -- see the module docstring.
    configured_in: str = ""
    #: What the reviewer should do. §8's whole justification for a condition
    #: existing is that there is something to do; writing it down is what keeps
    #: a condition from surviving past its usefulness.
    action: str = ""

    @property
    def externally_watched(self) -> bool:
        return self.probe is None


@dataclass(frozen=True, slots=True)
class Measurement:
    """What a probe observed."""

    value: float
    #: True when the value crosses the threshold and the alert should fire.
    firing: bool
    detail: str = ""
    #: Anything the alert body should carry beyond the number.
    context: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Alert:
    condition: Condition
    measurement: Measurement

    def render(self) -> str:
        state = "FIRING" if self.measurement.firing else "ok"
        return (
            f"[{state:>6}] {self.condition.key:28} "
            f"{self.measurement.value:>10.3g}  {self.measurement.detail}"
        )


# --- probes ----------------------------------------------------------------


def _content_queue_depth(session: Session) -> Measurement:
    depth = int(
        session.execute(
            sql(
                "SELECT count(*) FROM content_review_queue "
                " WHERE status IN ('pending', 'in_review')"
            )
        ).scalar_one()
    )
    limit = THRESHOLDS["review_queue_depth"]
    return Measurement(
        value=depth,
        firing=depth > limit,
        detail=f"{depth} unresolved content review items (threshold {limit:.0f})",
    )


def _ingestion_queue_depth(session: Session) -> Measurement:
    depth = int(
        session.execute(
            sql(
                "SELECT count(*) FROM ingestion_review_queue "
                " WHERE status IN ('pending', 'in_review')"
            )
        ).scalar_one()
    )
    limit = THRESHOLDS["ingestion_queue_depth"]
    return Measurement(
        value=depth,
        firing=depth > limit,
        detail=f"{depth} unresolved ingestion review items (threshold {limit:.0f})",
    )


def _postgres_storage(session: Session) -> Measurement:
    """§8.1: Postgres storage above 80% of the 40GB volume.

    ``pg_database_size`` rather than the volume's own free space: the backend
    has no view of the Fly volume Postgres runs on, and the database is what
    grows. It under-reports -- WAL, temp files and the filesystem's own
    overhead are outside it -- which is the direction to be wrong in for a
    threshold whose response time is a week.
    """
    size_bytes = int(
        session.execute(sql("SELECT pg_database_size(current_database())")).scalar_one()
    )
    volume_bytes = POSTGRES_VOLUME_GB * 1024**3
    fraction = size_bytes / volume_bytes
    limit = THRESHOLDS["postgres_storage_fraction"]
    return Measurement(
        value=fraction,
        firing=fraction > limit,
        detail=(
            f"{size_bytes / 1024 ** 3:.1f} GB of {POSTGRES_VOLUME_GB} GB "
            f"({fraction:.0%}); WAL and temp files are not counted"
        ),
        context={"size_bytes": size_bytes, "volume_gb": POSTGRES_VOLUME_GB},
    )


def _cost_trend(session: Session, *, today: dt.date | None = None) -> Measurement:
    """§8.1 and §13.3: extrapolated month-end spend above 90% of the cap.

    Straight-line from the month to date. A better model would weight recent
    days, and would be a worse fit for the thing being predicted: at MVP one
    learner's usage is lumpy enough that a weighted extrapolation swings by
    more than the signal. The alert's response window is a week, so a crude
    estimate a week early is the right trade.

    Extrapolating from a partial first day is the one case that misleads
    badly -- $2 spent by 09:00 on the 1st projects to $60 for the month -- so
    the first day of the month never fires. §13.3's response window absorbs the
    delay.
    """
    today = today or dt.date.today()
    day_of_month = today.day

    row = session.execute(
        sql(
            """
            SELECT COALESCE(SUM(cl.cost_usd), 0) AS spent,
                   COALESCE(MAX(c.monthly_hard_usd), 0) AS cap
              FROM cost_ledger cl
              FULL JOIN user_budget_caps c ON TRUE
             WHERE cl.day >= date_trunc('month', CAST(:today AS date))::date
               AND cl.day <= CAST(:today AS date)
            """
        ),
        {"today": today},
    ).one()
    spent = float(row.spent or 0)
    # Sum of every learner's hard cap is the ceiling the operator actually
    # cares about, not any one learner's.
    cap = float(
        session.execute(
            sql("SELECT COALESCE(SUM(monthly_hard_usd), 0) FROM user_budget_caps")
        ).scalar_one()
    )

    days_in_month = _days_in_month(today)
    projected = (spent / day_of_month) * days_in_month if day_of_month else 0.0
    fraction = projected / cap if cap else 0.0
    limit = THRESHOLDS["month_end_cost_fraction_of_cap"]

    return Measurement(
        value=fraction,
        firing=day_of_month > 1 and cap > 0 and fraction > limit,
        detail=(
            f"${spent:.2f} in {day_of_month} day(s) projects to ${projected:.2f} "
            f"against a ${cap:.2f} cap ({fraction:.0%})"
            + ("; day 1 never fires" if day_of_month <= 1 else "")
        ),
        context={"spent_usd": spent, "projected_usd": projected, "cap_usd": cap},
    )


def _days_in_month(day: dt.date) -> int:
    import calendar

    return calendar.monthrange(day.year, day.month)[1]


def _unattributed_content(session: Session) -> Measurement:
    """Not a §8.1 row. Closes SPEC_DEBT SD2.

    ``cost_rollup.roll_up_day`` returns an ``unattributed_content`` count --
    artifacts whose cost booked to the system account because no session could
    be attributed. SD2's complaint was that the figure "reports into a void":
    its only production caller discarded it, and a mitigation nobody reads is
    indistinguishable from no mitigation. SD2 names subsystem 7 as the owner,
    because "a line on whatever operational dashboard subsystem 7 defines" is
    the fix, and this is that line.

    A *ratio*, not a count. Curator pre-generation legitimately has no session
    and is expected to be a standing fraction of content cost; alerting on the
    absolute number would fire on a productive week of authoring. What is worth
    a look is the fraction moving, which is what a pipeline that stopped
    populating ``generated_for_session_id`` looks like.
    """
    row = session.execute(
        sql(
            """
            SELECT count(*) FILTER (WHERE generated_for_session_id IS NULL) AS orphan,
                   count(*) AS total
              FROM content_artifacts
             WHERE cost_usd IS NOT NULL
               AND generated_at >= NOW() - INTERVAL '7 days'
            """
        )
    ).one()
    total = int(row.total or 0)
    orphan = int(row.orphan or 0)
    ratio = orphan / total if total else 0.0
    limit = THRESHOLDS["unattributed_content_ratio"]
    return Measurement(
        value=ratio,
        firing=total > 0 and ratio > limit,
        detail=(
            f"{orphan} of {total} artifacts in 7 days booked to the system "
            f"account for want of a session ({ratio:.0%})"
        ),
        context={"orphan": orphan, "total": total},
    )


def _stale_retention_worker(session: Session) -> Measurement:
    """Not a §8.1 row either. The worker's own silence.

    §12.1 fires the pass daily at 02:00 UTC. A worker that stops -- an
    exception in the scheduler task, a deploy that lost the environment flag --
    produces no error and no alert: the tables simply stop shrinking, which is
    invisible until §8.1's storage condition fires weeks later with the wrong
    diagnosis attached. ``retention_actions`` makes the absence measurable, so
    it is measured.

    48 hours rather than 24: one missed pass is a restart, two is a fault.
    """
    latest = session.execute(
        sql("SELECT max(ran_at) FROM retention_actions")
    ).scalar()
    if latest is None:
        return Measurement(
            value=-1.0,
            firing=False,
            detail="the retention worker has never run; nothing to compare against",
        )
    age_hours = (dt.datetime.now(dt.UTC) - latest).total_seconds() / 3600
    return Measurement(
        value=age_hours,
        firing=age_hours > 48,
        detail=f"last retention pass {age_hours:.1f}h ago (§12.1 fires daily)",
        context={"last_run": latest.isoformat()},
    )


# --- the §8.1 table --------------------------------------------------------

CONDITIONS: tuple[Condition, ...] = (
    Condition(
        key="uptime_failure",
        description="The public health endpoint stopped answering.",
        threshold="2 consecutive 5-minute pings fail",
        delivery="email + push",
        response_within=RESPONSE_IMMEDIATE,
        configured_in=(
            "Uptime Robot (§7.4): HTTP monitor on https://studium.app/health, "
            "5-minute interval, alert after 2 failures"
        ),
        action="Check the Fly status page, then `flyctl logs`. §15's first two "
        "rows cover region outage and Postgres failure.",
    ),
    Condition(
        key="sentry_error_rate_spike",
        description="Unhandled exceptions well above baseline.",
        threshold="10x baseline over 15 minutes",
        delivery="email",
        response_within=RESPONSE_1H,
        configured_in="Sentry alert rule, backend + frontend projects (§7.2)",
        action="Read the top issue. A recent deploy is the usual cause; "
        "`flyctl releases rollback` is 30 seconds (§10.4).",
    ),
    Condition(
        key="budget_exceeded_error",
        description="A learner hit a hard spend cap.",
        threshold="any occurrence",
        delivery="email",
        response_within=RESPONSE_4H,
        configured_in="Sentry issue alert on BudgetExceededError (§7.2)",
        action="§15: raise the cap, tighten per-user limits, or find the "
        "runaway loop. `studium ops cost-report` says which.",
    ),
    Condition(
        key="missing_provenance",
        description="An ingestion write reached the database with no provenance.",
        threshold="any occurrence",
        delivery="email",
        response_within=RESPONSE_24H,
        configured_in="Sentry issue alert on MissingProvenance (§7.2)",
        action="Ingestion §13's invariant refused a write. The row is in "
        "ingestion_review_queue; `studium queue show` has it.",
    ),
    Condition(
        key="content_review_queue_depth",
        description="Generated content is waiting on a reviewer.",
        threshold="> 20 pending items",
        delivery="email (daily digest)",
        response_within=RESPONSE_48H,
        probe=_content_queue_depth,
        action="`studium review list`.",
    ),
    Condition(
        key="ingestion_review_queue_depth",
        description="Ingestion failures are waiting on a reviewer.",
        threshold="> 20 pending items",
        delivery="email (daily digest)",
        response_within=RESPONSE_48H,
        probe=_ingestion_queue_depth,
        action="`studium queue list`.",
    ),
    Condition(
        key="postgres_storage",
        description="The database is filling its volume.",
        threshold="> 80% of the 40GB volume",
        delivery="email",
        response_within=RESPONSE_1W,
        probe=_postgres_storage,
        action="§14.2: read replicas first, then tune, then resize. Check the "
        "retention worker is running before resizing anything.",
    ),
    Condition(
        key="vm_memory",
        description="The backend VM is close to its 2GB.",
        threshold="> 90% sustained 15 minutes",
        delivery="email",
        response_within=RESPONSE_4H,
        configured_in="Fly.io metrics alert on the backend app (§7.4)",
        action="§14.1's scaling trigger, or a leak. The embedding worker and "
        "the cache warmer are the two things that hold memory.",
    ),
    Condition(
        key="anthropic_5xx_rate",
        description="The model provider is failing requests.",
        threshold="> 5% over 15 minutes",
        delivery="email",
        response_within=RESPONSE_IMMEDIATE,
        configured_in="Sentry metric alert on APIStatusError, filtered to 5xx (§7.2)",
        action="Nothing but wait (§15). Agents surface graceful errors; the "
        "degradation copy is already learner-facing.",
    ),
    Condition(
        key="voyage_5xx_rate",
        description="The embedding provider is failing requests.",
        threshold="> 5% over 15 minutes",
        delivery="email",
        response_within=RESPONSE_IMMEDIATE,
        configured_in="Sentry metric alert on the Voyage client's error path (§7.2)",
        action="Retrieval falls back to keyword-only (retrieval §16). Quality "
        "is degraded, not broken.",
    ),
    Condition(
        key="evaluation_regression",
        description="A golden dataset regressed beyond its tolerance.",
        threshold="any dataset regressing beyond tolerance",
        delivery="email",
        response_within=RESPONSE_1W,
        configured_in=(
            ".github/workflows/prompt-regression.yml (§10.2) -- a failing job "
            "is the alert; GitHub emails the actor"
        ),
        action="evaluation §13.1 step 7: approve with a documented reason, "
        "request changes, or reject.",
    ),
    Condition(
        key="cost_trending_to_cap",
        description="Month-end LLM spend is projected past the cap.",
        threshold="extrapolated month-end > 90% of cap",
        delivery="email (daily)",
        response_within=RESPONSE_1W,
        probe=_cost_trend,
        action="`studium ops cost-report` for the breakdown by user and cost "
        "line; §13.1's anomaly column names the outlier.",
    ),
    # --- additions to §8.1, each closing a named gap -----------------------
    Condition(
        key="unattributed_content",
        description="Content cost is booking to the system account (SPEC_DEBT SD2).",
        threshold="> 20% of 7-day content artifacts have no triggering session",
        delivery="email (daily digest)",
        response_within=RESPONSE_1W,
        probe=_unattributed_content,
        action="Check that the content pipeline still sets "
        "content_artifacts.generated_for_session_id. Until it does, every "
        "cost report understates per-learner spend.",
    ),
    Condition(
        key="retention_worker_stale",
        description="The §12.1 nightly pass has not run.",
        threshold="no retention_actions row in 48 hours",
        delivery="email",
        response_within=RESPONSE_1W,
        probe=_stale_retention_worker,
        action="Check STUDIUM_RETENTION_WORKER is 1 on the backend app and "
        "read the scheduler's log line. A silent worker looks exactly like a "
        "system with nothing to delete.",
    ),
)

BY_KEY: dict[str, Condition] = {c.key: c for c in CONDITIONS}

#: §8.2, verbatim in substance. Kept as data so the Tier 1 test can assert no
#: key appears in both lists: a condition that migrates from here into
#: CONDITIONS without the reasoning being revisited is how alert fatigue starts.
NOT_ALERTED: tuple[tuple[str, str], ...] = (
    ("failed_retrieval", "thin grounding is a queue item, not an alert"),
    (
        "failed_agent_call",
        "retries handle these; only sustained failure patterns alert, which is "
        "what sentry_error_rate_spike measures",
    ),
    (
        "learner_visible_degradation",
        "the learner sees it and the reviewer sees the queue item; alerting on "
        "each would produce noise",
    ),
    (
        "session_termination",
        "idle timeouts, budget caps and learner-initiated closes are normal",
    ),
    ("preview_deploy_failure", "the developer sees it in their own workflow"),
)


def evaluate(session: Session, *, keys: Iterable[str] | None = None) -> list[Alert]:
    """Run every database-backed probe. Returns one Alert per condition run.

    Non-firing conditions are returned too. ``check-alerts`` prints them, and a
    run that reported only failures could not distinguish "nothing is wrong"
    from "the probe raised and was swallowed".
    """
    wanted = set(keys) if keys is not None else None
    results: list[Alert] = []
    for condition in CONDITIONS:
        if condition.probe is None:
            continue
        if wanted is not None and condition.key not in wanted:
            continue
        results.append(Alert(condition, condition.probe(session)))
    return results


def firing(alerts: Iterable[Alert]) -> list[Alert]:
    return [a for a in alerts if a.measurement.firing]
