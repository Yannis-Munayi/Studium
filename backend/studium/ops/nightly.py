"""The nightly maintenance pass (infrastructure §12.1, and three orphans).

§12.1 asks for one scheduled task: the retention worker, daily at 02:00 UTC.
Building it turned up that **three other jobs were written to run on a schedule
and had no caller at all**. This module is the schedule they were missing.

An inventory, taken with
``grep -rn "purge_expired_soft_deletes\\|refresh_decay\\|check_dangling_chunk_refs"
studium/ tests/`` before writing any of this:

===================================  ====================================
``privacy.purge_expired_soft_deletes``  Data layer §10 step 4. Called by one
                                        test and nothing else. **An erasure
                                        request was therefore never
                                        completing**: the account is marked
                                        deleted, the 30-day dispute window
                                        expires, and the row stays forever
                                        because nothing came back for it.
``cost_rollup.refresh_decay``           Docstring says "Daily". Called by one
                                        test. ``concept_mastery
                                        .p_known_decayed`` was frozen at
                                        whatever it was when last written.
                                        Gating is unaffected -- ``graph
                                        .unlock_status`` computes decay in
                                        SQL -- but every sort and every report
                                        that reads the column was reading a
                                        stale number.
``retention.check_dangling_chunk_refs`` Docstring says "Daily consistency
                                        check". Called by nothing at all. A
                                        ``concept_sources.chunk_ids`` entry
                                        pointing at a deleted chunk degrades
                                        silently by design (data layer §6.2:
                                        the array cannot carry a foreign key),
                                        so silence was the only symptom.
===================================  ====================================

Same shape all three times, and the same shape as the four instances in
``SPEC_DEBT``'s "later subsystems find earlier defects": a function that
appears in every declaration site -- exported from ``studium.jobs``, described
as scheduled in its own docstring, covered by a passing test -- and has no
execution site. **The first consumer of a code path is what proves it exists.**
Subsystem 7 owns the scheduler, so subsystem 7 is where they get one.

**Each stage is independent and failure-isolated.** A stage that raises is
recorded and the pass continues; one bad predicate must not stop the erasure
purge, and none of them may take down the process serving learners.

**Ordering is deliberate.** Retention first, because it is the stage with a
window to hit and the one whose backlog grows. The erasure purge second, so a
learner's hard delete is not waiting behind a decay refresh. Decay third, since
nothing blocks on it. The consistency check last, because it only reports.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


@dataclass(slots=True)
class StageResult:
    name: str
    detail: str = ""
    duration_ms: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def render(self) -> str:
        if self.error:
            return f"  {self.name:22} FAILED  {self.error}"
        return f"  {self.name:22} {self.duration_ms:>6} ms  {self.detail}"


@dataclass(slots=True)
class NightlyResult:
    run_id: uuid.UUID
    started_at: dt.datetime
    stages: list[StageResult] = field(default_factory=list)

    @property
    def failures(self) -> list[StageResult]:
        return [s for s in self.stages if not s.ok]

    def render(self) -> str:
        lines = [
            f"nightly maintenance {self.run_id}",
            f"  started {self.started_at.isoformat()}",
            "",
        ]
        lines += [s.render() for s in self.stages]
        lines.append("")
        lines.append(
            f"  {len(self.stages)} stage(s), {len(self.failures)} failure(s)"
        )
        return "\n".join(lines)

    def summary(self) -> str:
        """One line, for the scheduler's log and /health."""
        parts = [f"{s.name}={s.detail or 'ok'}" for s in self.stages if s.ok]
        if self.failures:
            parts.append(f"FAILED: {', '.join(s.name for s in self.failures)}")
        return "; ".join(parts)


def run_nightly(session: Session, *, dry_run: bool = False) -> NightlyResult:
    """Every scheduled maintenance job, once. Never raises."""
    result = NightlyResult(run_id=uuid.uuid4(), started_at=dt.datetime.now(dt.UTC))

    for name, stage in (
        ("retention", _retention),
        ("erasure_purge", _erasure_purge),
        ("mastery_decay", _mastery_decay),
        ("consistency", _consistency),
    ):
        started = time.perf_counter()
        try:
            detail = stage(session, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 -- recorded, not raised
            session.rollback()
            log.exception("nightly stage %s failed", name)
            result.stages.append(
                StageResult(
                    name=name,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
        else:
            result.stages.append(
                StageResult(
                    name=name,
                    detail=detail,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )

    return result


def _retention(session: Session, *, dry_run: bool) -> str:
    """§12.1's pass. Writes its own per-policy audit rows."""
    from .retention import run_retention

    run = run_retention(session, dry_run=dry_run)
    suffix = f", {len(run.failures)} policy failure(s)" if run.failures else ""
    return f"{run.rows_deleted} row(s) across {len(run.results)} policies{suffix}"


def _erasure_purge(session: Session, *, dry_run: bool) -> str:
    """Data layer §10 step 4: hard-delete accounts past the dispute window.

    Without this stage an erasure request never finishes. The account is
    soft-deleted, the 30 days expire, and the row stays -- so the learner who
    asked to be forgotten is still in ``users``, with an email address, for as
    long as the deployment lives.

    The anonymised rows in ``assessment_attempts``, ``cost_ledger`` and
    ``audit_log`` survive it: their owner columns are already NULL, so the
    cascade has nothing to follow.
    """
    from sqlalchemy import text as sql

    from studium.privacy import DISPUTE_WINDOW_DAYS, purge_expired_soft_deletes

    if dry_run:
        due = int(
            session.execute(
                sql(
                    f"""
                    SELECT count(*) FROM users
                     WHERE deleted_at IS NOT NULL
                       AND deleted_at < NOW() - INTERVAL '{DISPUTE_WINDOW_DAYS} days'
                    """
                )
            ).scalar_one()
        )
        return f"{due} account(s) due for hard deletion (dry run)"

    purged = purge_expired_soft_deletes(session)
    return f"{purged} account(s) hard-deleted past the {DISPUTE_WINDOW_DAYS}-day window"


def _mastery_decay(session: Session, *, dry_run: bool) -> str:
    """Recompute ``concept_mastery.p_known_decayed``.

    Not load-bearing for gating -- ``graph.unlock_status`` computes decay in
    SQL at query time -- which is exactly why nobody noticed the column was
    stale. What reads it is sorting and reporting, so the symptom of its
    absence is a desk that orders concepts by a number from whenever the
    learner last touched them.
    """
    if dry_run:
        return "skipped in dry run (it is a full-table UPDATE)"
    from studium.jobs.cost_rollup import refresh_decay

    return f"{refresh_decay(session)} mastery row(s) refreshed"


def _consistency(session: Session, *, dry_run: bool) -> str:
    """Data layer §6.2's dangling ``chunk_ids``, flagged rather than logged.

    ``check_dangling_chunk_refs`` has existed since the data layer build and
    has never had a caller. It returns ``concept_sources`` ids whose
    ``chunk_ids`` array points at chunks that are gone -- which the array
    cannot prevent, because Postgres will not put a foreign key on an array
    element.

    Flagged into ``ingestion_review_queue`` rather than logged. That table is
    the surface SPEC_DEBT SD1 asked for and ingestion §12 built; logging is
    precisely the stopgap SD1 was written to complain about, and a warning
    nobody greps for is only marginally better than a discarded return value.

    Severity 1: nothing is broken. Retrieval degrades gracefully past a missing
    chunk by design. It is worth a look because the usual cause is a
    re-extraction that superseded chunks without repointing the concept.

    De-duplicated against open rows, because this runs every night and a
    dangling reference nobody has fixed would otherwise produce 365 identical
    queue items a year.
    """
    from sqlalchemy import text as sql

    from studium.jobs.retention import check_dangling_chunk_refs

    dangling = check_dangling_chunk_refs(session)
    if not dangling:
        return "no dangling concept_sources.chunk_ids"
    if dry_run:
        return f"{len(dangling)} dangling chunk reference(s) (dry run, nothing queued)"

    from studium.ingestion.queue import flag

    queued = 0
    for concept_source_id in dangling:
        row = session.execute(
            sql(
                "SELECT concept_id, source_id FROM concept_sources WHERE id = :id"
            ),
            {"id": concept_source_id},
        ).one_or_none()
        if row is None:  # pragma: no cover -- deleted between the two queries
            continue

        already_open = session.execute(
            sql(
                """
                SELECT 1 FROM ingestion_review_queue
                 WHERE flag_source = 'concept_source_conflict'
                   AND status IN ('pending', 'in_review')
                   AND payload ->> 'concept_source_id' = :csid
                 LIMIT 1
                """
            ),
            {"csid": str(concept_source_id)},
        ).scalar()
        if already_open:
            continue

        flag(
            session,
            flag_source="concept_source_conflict",
            severity=1,
            reason=(
                "concept_sources.chunk_ids points at chunks that no longer "
                "exist. Retrieval skips them silently (data layer §6.2: the "
                "array cannot carry a foreign key), so the concept is grounded "
                "in less evidence than its mapping claims."
            ),
            concept_id=row.concept_id,
            source_id=row.source_id,
            payload={
                "concept_source_id": str(concept_source_id),
                "found_by": "nightly consistency check (infrastructure §12.1)",
            },
        )
        queued += 1

    session.commit()
    return (
        f"{len(dangling)} dangling chunk reference(s), {queued} newly queued "
        f"for review"
    )
