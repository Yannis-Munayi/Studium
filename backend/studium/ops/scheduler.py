"""The in-process nightly scheduler (infrastructure §12.1).

§12.1: "Runs as a scheduled task (cron-like) in the backend process. Fires
daily at 02:00 UTC."

**In-process rather than a Fly cron machine**, which is the choice §4.2 already
made by sizing one VM to hold "the FastAPI process, embedding worker, ingestion
worker, and the scheduled retention/warming jobs". A separate scheduler machine
would need its own image, its own secrets, its own deploy step and its own
alerting, to run three statements a night against a single-node database. The
cost of the choice is real and worth naming: **a single-instance assumption**.
The moment §14.1's auto-scaling trigger fires, three backend instances mean
three retention passes at 02:00, and the guard for that is the advisory lock
below rather than a promise to remember.

**Off by default.** ``STUDIUM_RETENTION_WORKER=1`` switches it on, for the
reason retrieval's cache warmer is off by default: a developer running the API
to look at one endpoint, or a test that constructs the app, must not start a
task whose job is deleting rows. The deployment sets it; nothing else does.

**Nothing here can fail a request.** The task catches everything, logs, and
sleeps until tomorrow. A retention pass that raises must not take down the
process serving learners.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os

log = logging.getLogger(__name__)

#: §12.1's hour, UTC. 02:00 Toronto-time is 06:00/07:00 UTC depending on DST,
#: and a job whose start time moves twice a year is a job whose "did it run"
#: query needs a special case. UTC is what §12.1 says and what the audit rows
#: are stamped in.
RETENTION_HOUR_UTC = 2

ENABLE_ENV = "STUDIUM_RETENTION_WORKER"

#: One advisory lock id, arbitrary and fixed. Postgres session-level advisory
#: locks are the cheapest correct answer to "only one instance runs this":
#: they cost no table, they are released automatically if the holder dies, and
#: `pg_try_advisory_lock` returns rather than waits, so a second instance skips
#: the pass instead of running it an hour late.
RETENTION_LOCK_ID = 0x5D_17_12_01


def seconds_until_next_run(now: dt.datetime | None = None, *, hour: int = RETENTION_HOUR_UTC) -> float:
    """Seconds from ``now`` to the next ``hour``:00 UTC.

    Exactly-on-the-hour counts as *next* day, not now: a process that starts at
    02:00:00.000 would otherwise run immediately and again in 24 hours, which
    is two passes in one night for the one deploy that lands at the wrong
    second.
    """
    now = now or dt.datetime.now(dt.UTC)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


def _run_pass() -> str:
    """One nightly pass, holding the advisory lock. Returns a log line.

    Runs :func:`studium.ops.nightly.run_nightly`, which is retention plus the
    three jobs that were written to run on a schedule and had no caller. See
    that module's docstring for the inventory.
    """
    from sqlalchemy import text as sql

    from studium.ops import owner_session
    from studium.ops.nightly import run_nightly

    with owner_session() as session:
        acquired = bool(
            session.execute(
                sql("SELECT pg_try_advisory_lock(:id)"), {"id": RETENTION_LOCK_ID}
            ).scalar()
        )
        if not acquired:
            return "another instance holds the nightly lock; skipping this pass"
        try:
            result = run_nightly(session)
            for failure in result.failures:
                log.error("nightly stage %s failed: %s", failure.name, failure.error)
            return f"nightly {result.run_id}: {result.summary()}"
        finally:
            session.execute(
                sql("SELECT pg_advisory_unlock(:id)"), {"id": RETENTION_LOCK_ID}
            )
            session.commit()


class RetentionScheduler:
    """Fires :func:`studium.ops.retention.run_retention` daily at 02:00 UTC."""

    def __init__(self, *, hour: int = RETENTION_HOUR_UTC) -> None:
        self.hour = hour
        self._task: asyncio.Task[None] | None = None
        self._last_run: dt.datetime | None = None
        self._last_result: str = "never run"

    def start(self) -> None:
        if self._task is not None:  # pragma: no cover -- guarded by lifespan
            return
        self._task = asyncio.create_task(self._loop(), name="retention-scheduler")
        log.info(
            "retention worker armed for %02d:00 UTC daily (next in %.1f h)",
            self.hour,
            seconds_until_next_run(hour=self.hour) / 3600,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(seconds_until_next_run(hour=self.hour))
            try:
                # A thread, because run_retention is synchronous SQLAlchemy and
                # a pass over a year's traces would otherwise block the event
                # loop -- and the event loop is where learners' streams live.
                self._last_result = await asyncio.to_thread(_run_pass)
                self._last_run = dt.datetime.now(dt.UTC)
                log.info("%s", self._last_result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- must not kill the app
                self._last_result = f"failed: {type(exc).__name__}: {exc}"
                log.exception("retention pass raised; the app keeps serving")

    def status(self) -> dict[str, object]:
        """What ``GET /health`` reports about the worker.

        A background job that fails silently and shows up as "storage is full"
        three weeks later is exactly what §7 means by observability designed
        in. ``alerts.retention_worker_stale`` reads the database side of the
        same question, which is the one that survives a restart.
        """
        return {
            "running": self._task is not None and not self._task.done(),
            "hour_utc": self.hour,
            "next_run_in_seconds": round(seconds_until_next_run(hour=self.hour)),
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "last_result": self._last_result,
        }
