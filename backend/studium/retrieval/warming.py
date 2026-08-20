"""Cache pre-warming for load-bearing concepts (retrieval §14).

A cold retrieval costs a reranker round trip inside the turn a learner is
waiting on. §14's answer is to pay that cost in the background, ahead of time,
for the small set of concepts most likely to be asked for first: the ones an
author marked ``is_load_bearing``.

    warmer = CacheWarmer(retriever)
    await warmer.warm_once()          # one pass, returns what it did
    await warmer.run_forever()        # rolling refresh on a 5-minute schedule

**It refuses to spend more than it was budgeted.** §15 prices continuous
warming of a single-subject deployment at roughly $35/month and notes that at
university scale "pre-warming becomes a real line item". A loop that silently
scaled with the concept count would discover that the expensive way, so the
number of concepts warmed per cycle is capped and the cap is the first thing
§14 says to tune.

**A failure never propagates.** Warming is an optimisation: if it stops
working, retrieval is slower and nothing else changes. Every exception is
caught and counted rather than allowed to kill the loop, because a background
task that dies silently on its first transient database error is worse than no
background task at all -- the latency regression it causes looks like a
retrieval problem, not a scheduler one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field

from studium.asyncdb import read_db

from .cache import load_bearing_concepts
from .types import DEFAULT_K

log = logging.getLogger(__name__)

#: §14: "A background job re-warms these on a rolling 5-minute schedule."
#: Matches the cache TTL, so an entry is replaced about when it would expire.
WARM_INTERVAL_SECONDS = 300.0

#: §14 warms with the default stance at the Lecturer's default k, because that
#: is the shape of the call a session's first segment actually makes.
WARM_STANCE = "default"

#: Ceiling per cycle. At MVP (§14: ~11 concepts, 3-4 load-bearing) this never
#: binds. It exists so that enabling warming on a large corpus degrades to
#: "warms the first N" rather than to an unbounded hourly bill.
MAX_CONCEPTS_PER_CYCLE = 24

#: Gap between calls within a cycle. Warming is never urgent, and firing two
#: dozen reranker calls at once competes with the learners the warming exists
#: to help.
STAGGER_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class WarmReport:
    """What one warming pass did. Returned so a caller can log or assert on it."""

    warmed: int = 0
    failed: int = 0
    skipped: int = 0
    elapsed_seconds: float = 0.0

    @property
    def attempted(self) -> int:
        return self.warmed + self.failed


@dataclass
class CacheWarmer:
    """Runs §14's rolling refresh against a retriever's cache.

    Holds the retriever rather than a cache directly: warming means *doing* a
    retrieval, and the cache write is a side effect of that. Warming the cache
    any other way would store an entry the real pipeline never produced.
    """

    retriever: object
    interval: float = WARM_INTERVAL_SECONDS
    max_concepts: int = MAX_CONCEPTS_PER_CYCLE
    stagger: float = STAGGER_SECONDS
    #: Cumulative, across every cycle since start. Read by the health endpoint.
    cycles: int = 0
    total_warmed: int = 0
    total_failed: int = 0
    last_report: WarmReport | None = None
    _task: asyncio.Task | None = field(default=None, repr=False)

    async def concepts(self) -> list[uuid.UUID]:
        """The concepts to warm: load-bearing, in an active subject (§14)."""
        try:
            found = await read_db(load_bearing_concepts)
        except Exception:  # noqa: BLE001 -- warming must not raise
            log.exception("cache warming could not list load-bearing concepts")
            return []

        if len(found) > self.max_concepts:
            log.info(
                "warming the first %d of %d load-bearing concepts; raise "
                "MAX_CONCEPTS_PER_CYCLE deliberately, it is a cost control",
                self.max_concepts, len(found),
            )
        return found[: self.max_concepts]

    async def warm_once(self) -> WarmReport:
        """One pass over the load-bearing set."""
        started = time.monotonic()
        concepts = await self.concepts()
        if not concepts:
            report = WarmReport(elapsed_seconds=time.monotonic() - started)
            self.last_report = report
            return report

        warmed = failed = skipped = 0

        for index, concept_id in enumerate(concepts):
            if index and self.stagger:
                await asyncio.sleep(self.stagger)
            try:
                result = await self.retriever.retrieve_passages(  # type: ignore[attr-defined]
                    concept_id, stance=WARM_STANCE, k=DEFAULT_K
                )
            except Exception:  # noqa: BLE001 -- one bad concept is not fatal
                log.exception("warming failed for concept %s", concept_id)
                failed += 1
                continue

            # A degraded result is not cached by the retriever (S10), so warming
            # it achieved nothing and should be reported as such rather than
            # counted as a success the next cycle will silently repeat.
            if result.degraded:
                skipped += 1
            else:
                warmed += 1

        report = WarmReport(
            warmed=warmed,
            failed=failed,
            skipped=skipped,
            elapsed_seconds=time.monotonic() - started,
        )
        self.cycles += 1
        self.total_warmed += warmed
        self.total_failed += failed
        self.last_report = report

        log.info(
            "cache warming cycle %d: %d warmed, %d degraded, %d failed in %.1fs",
            self.cycles, warmed, skipped, failed, report.elapsed_seconds,
        )
        return report

    async def run_forever(self) -> None:
        """The rolling schedule. Cancel the task to stop it.

        Sleeps for the remainder of the interval rather than a flat interval,
        so a cycle that takes 90 seconds does not turn a 5-minute schedule into
        a 6.5-minute one -- the cache TTL is 5 minutes and the refresh has to
        stay inside it or entries expire before they are replaced.
        """
        while True:
            report = await self.warm_once()
            remaining = max(0.0, self.interval - report.elapsed_seconds)
            if remaining == 0.0:
                log.warning(
                    "a warming cycle took %.0fs, longer than the %.0fs interval; "
                    "entries are expiring before they are refreshed",
                    report.elapsed_seconds, self.interval,
                )
            await asyncio.sleep(remaining)

    def start(self) -> asyncio.Task:
        """Launch the loop as a background task. Idempotent."""
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self.run_forever(), name="retrieval-cache-warmer")
        log.info("cache warming started on a %.0fs schedule", self.interval)
        return self._task

    async def stop(self) -> None:
        """Cancel the loop and wait for it to unwind."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        log.info("cache warming stopped after %d cycles", self.cycles)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, object]:
        """Diagnostic snapshot, for the health endpoint."""
        report = self.last_report
        return {
            "running": self.running,
            "interval_seconds": self.interval,
            "cycles": self.cycles,
            "total_warmed": self.total_warmed,
            "total_failed": self.total_failed,
            "last_cycle": None
            if report is None
            else {
                "warmed": report.warmed,
                "degraded": report.skipped,
                "failed": report.failed,
                "elapsed_seconds": round(report.elapsed_seconds, 2),
            },
        }
