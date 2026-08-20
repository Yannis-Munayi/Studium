"""Session opening and closing (agent runtime §16).

Three phases: opening, body, closing. The body is the state machine (§7); these
are the one-shot procedures that bracket it.

Both are transactional at the step level rather than as a whole. A close that
fails while writing the summary must still have ended the session -- otherwise
the learner has an eternally open session holding budget, which is worse than a
missing summary. Each step logs and continues where continuing is safe, and the
report says which steps completed.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.agents.base import AgentInput
from studium.agents.schemas import SessionSummary
from studium.asyncdb import read_db, run_db
from studium.jobs.cost_rollup import roll_up_day
from studium.models import LearnerSubject, LearningSession
from studium.session.context import SessionContext

log = logging.getLogger(__name__)

#: §16 step 4: a prior summary older than this is not worth checking retention
#: against -- the retrieval check would measure long-term forgetting rather
#: than whether the last session stuck.
PRIOR_SESSION_WINDOW_DAYS = 7


class NoEnrollmentError(LookupError):
    """The learner has no active enrollment to open a session against."""


async def open_session(
    *,
    user_id: uuid.UUID,
    mode: str,
    focus_concept_id: uuid.UUID | None = None,
    learner_subject_id: uuid.UUID | None = None,
    target_duration_minutes: int = 90,
) -> uuid.UUID:
    """Create the ``learning_sessions`` row (§16 step 2).

    Resolves the enrollment when the caller did not name one. §20 allows at
    most one active session per learner, which is enforced here rather than by
    a constraint: the data layer has a partial index on active sessions but no
    uniqueness, since "active" is a business rule the schema deliberately does
    not encode.
    """
    return await run_db(
        lambda s: _open(
            s,
            user_id=user_id,
            mode=mode,
            focus_concept_id=focus_concept_id,
            learner_subject_id=learner_subject_id,
            target_duration_minutes=target_duration_minutes,
        )
    )


def _open(
    session: Session,
    *,
    user_id: uuid.UUID,
    mode: str,
    focus_concept_id: uuid.UUID | None,
    learner_subject_id: uuid.UUID | None,
    target_duration_minutes: int,
) -> uuid.UUID:
    if learner_subject_id is None:
        enrollment = session.execute(
            select(LearnerSubject)
            .where(LearnerSubject.user_id == user_id)
            .where(LearnerSubject.archived_at.is_(None))
            .order_by(LearnerSubject.enrolled_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if enrollment is None:
            raise NoEnrollmentError(f"user {user_id} has no active enrollment")
        learner_subject_id = enrollment.id
        focus_concept_id = focus_concept_id or enrollment.current_focus_concept_id
    else:
        enrollment = session.get(LearnerSubject, learner_subject_id)
        if enrollment is None:
            raise NoEnrollmentError(f"no enrollment {learner_subject_id}")
        focus_concept_id = focus_concept_id or enrollment.current_focus_concept_id

    active = session.execute(
        select(LearningSession.id)
        .where(LearningSession.user_id == user_id)
        .where(LearningSession.ended_at.is_(None))
        .limit(1)
    ).scalar_one_or_none()
    if active is not None:
        log.info("resuming existing active session %s for user %s", active, user_id)
        return active

    row = LearningSession(
        user_id=user_id,
        learner_subject_id=learner_subject_id,
        mode=mode,
        focus_concept_id=focus_concept_id,
        target_duration_minutes=target_duration_minutes,
    )
    session.add(row)
    session.flush()
    return row.id


def should_run_retrieval_check(context: SessionContext) -> bool:
    """§16 step 4's guard: a prior summary inside the window."""
    prior = context.prior_session_summary
    if not prior or not prior.get("ended_at"):
        return False
    ended = dt.datetime.fromisoformat(str(prior["ended_at"]))
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=dt.UTC)
    age_days = (dt.datetime.now(dt.UTC) - ended).total_seconds() / 86400
    return age_days <= PRIOR_SESSION_WINDOW_DAYS


async def record_retrieval_check(
    context: SessionContext,
    *,
    prompts: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    scores: list[dict[str, Any]],
) -> float:
    """Persist the session-open check and return its overall score (§16 4b).

    The score is the share of prompts graded ``correct``. Partial credit is
    not counted: this gates whether to move on or revisit, and "half remembered"
    is a reason to revisit.
    """
    correct = sum(1 for s in scores if s.get("verdict") == "correct")
    overall = correct / len(scores) if scores else 0.0

    prior = context.prior_session_summary or {}

    def write(session: Session) -> None:
        session.execute(
            sql(
                """
                INSERT INTO retrieval_checks
                    (session_id, based_on_session_id, prompts, responses,
                     scores, overall_score)
                VALUES
                    (:session_id, :based_on, CAST(:prompts AS jsonb),
                     CAST(:responses AS jsonb), CAST(:scores AS jsonb), :overall)
                """
            ),
            {
                "session_id": context.session_id,
                "based_on": prior.get("session_id"),
                "prompts": _json(prompts),
                "responses": _json(responses),
                "scores": _json(scores),
                "overall": overall,
            },
        )

    await run_db(write)
    return overall


@dataclass(slots=True)
class CloseReport:
    """What actually happened during close, step by step.

    Returned rather than logged only, so a caller (and a test) can assert on
    which steps completed instead of grepping output.
    """

    session_id: uuid.UUID
    ended: bool = False
    summarised: bool = False
    decay_refreshed: int = 0
    focus_updated: bool = False
    cost_rolled: bool = False
    errors: list[str] = field(default_factory=list)


async def close_session(
    context: SessionContext,
    agents: Any,
    *,
    reason: str = "learner_stop",
) -> CloseReport:
    """Run CLOSING (§16).

    Steps run in the spec's order and are individually fault-tolerant: ending
    the session is what must not fail, and everything after it is recoverable
    by re-running.
    """
    report = CloseReport(session_id=context.session_id)

    # 1. End the session. This one is load-bearing: a session that never ends
    #    keeps holding budget and blocks the next one from opening.
    await run_db(lambda s: _mark_ended(s, context.session_id, reason))
    report.ended = True

    # 2. Summary.
    try:
        summary = await agents.curator.handle(
            AgentInput(session_context=context, kind="summarize_session", payload={})
        )
        await _write_summary(context, summary.structured, summary.trace)
        report.summarised = True
    except Exception as exc:  # noqa: BLE001
        log.exception("session summary failed for %s", context.session_id)
        report.errors.append(f"summary: {exc}")

    # 3. Refresh decay on the concepts this session touched.
    try:
        report.decay_refreshed = await run_db(
            lambda s: _refresh_touched_decay(s, context.session_id)
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("decay refresh failed for %s", context.session_id)
        report.errors.append(f"decay: {exc}")

    # 4. Move the enrollment's focus to whatever comes next.
    try:
        report.focus_updated = await _update_focus(context, agents)
    except Exception as exc:  # noqa: BLE001
        log.exception("focus update failed for %s", context.session_id)
        report.errors.append(f"focus: {exc}")

    # 5. Roll today's cost forward. The job is idempotent -- it recomputes each
    #    category from source rather than adding -- so running it here and
    #    again nightly cannot double-count (§19 vs §16 step 5).
    try:
        counts = await run_db(lambda s: roll_up_day(s, dt.datetime.now(dt.UTC).date()))
        report.cost_rolled = True

        # The roll-up's own signal that data-layer V2's attribution gap is
        # being hit: artifacts whose cost booked to the system account because
        # no session could be attributed. The count was previously discarded
        # here, which made the mitigation indistinguishable from its absence.
        # A log line is the floor, not the answer -- see SPEC_DEBT.md (SD2),
        # which holds this open for subsystem 7's operational surface.
        unattributed = int(counts.get("unattributed_content", 0))
        if unattributed:
            log.warning(
                "cost roll-up booked %d content artifact(s) to the system "
                "account for want of an attributable session (SD2)",
                unattributed,
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("cost roll-up failed for %s", context.session_id)
        report.errors.append(f"cost: {exc}")

    log.info("session %s closed: %s", context.session_id, report)
    return report


def _mark_ended(session: Session, session_id: uuid.UUID, reason: str) -> None:
    session.execute(
        sql(
            """
            UPDATE learning_sessions
               SET ended_at = NOW(), end_reason = :reason
             WHERE id = :session_id AND ended_at IS NULL
            """
        ),
        {"session_id": session_id, "reason": reason},
    )


async def _write_summary(
    context: SessionContext, summary: Any, trace: Any
) -> None:
    """Upsert ``session_summaries``. Regenerable, so upsert rather than insert."""
    if not isinstance(summary, SessionSummary):
        raise TypeError(f"expected SessionSummary, got {type(summary).__name__}")

    touched = await read_db(lambda s: _concepts_touched(s, context.session_id))

    def write(session: Session) -> None:
        session.execute(
            sql(
                """
                INSERT INTO session_summaries
                    (session_id, summary, key_points, open_threads,
                     concepts_touched, generated_by, cost_usd)
                VALUES
                    (:session_id, :summary, CAST(:key_points AS jsonb),
                     CAST(:open_threads AS jsonb),
                     CAST(:concepts AS uuid[]), 'curator', :cost)
                ON CONFLICT (session_id) DO UPDATE
                   SET summary          = EXCLUDED.summary,
                       key_points       = EXCLUDED.key_points,
                       open_threads     = EXCLUDED.open_threads,
                       concepts_touched = EXCLUDED.concepts_touched,
                       generated_by     = EXCLUDED.generated_by,
                       cost_usd         = EXCLUDED.cost_usd
                """
            ),
            {
                "session_id": context.session_id,
                "summary": summary.summary,
                "key_points": _json(summary.key_points),
                "open_threads": _json(summary.open_threads),
                "concepts": touched,
                "cost": str(trace.cost_usd) if trace else "0",
            },
        )

    await run_db(write)


def _concepts_touched(session: Session, session_id: uuid.UUID) -> list[uuid.UUID]:
    rows = session.execute(
        sql(
            """
            SELECT DISTINCT concept_id
              FROM session_turns
             WHERE session_id = :session_id AND concept_id IS NOT NULL
            """
        ),
        {"session_id": session_id},
    ).scalars()
    return list(rows)


def _refresh_touched_decay(session: Session, session_id: uuid.UUID) -> int:
    """§16 step 3. Scoped to this session's concepts, not the whole table.

    ``studium.jobs.cost_rollup.refresh_decay`` rewrites every row and belongs
    to the nightly job; at session close only what moved needs refreshing.
    """
    from studium.mastery import DECAY_HALF_LIFE

    result = session.execute(
        sql(
            """
            UPDATE concept_mastery cm
               SET p_known_decayed = CASE
                     WHEN cm.last_evidence_at IS NULL THEN cm.p_known
                     ELSE cm.p_known
                          * pow(0.5, EXTRACT(EPOCH FROM (NOW() - cm.last_evidence_at))
                                     / :half_life_seconds)
                   END
             WHERE cm.concept_id IN (
                       SELECT DISTINCT concept_id
                         FROM session_turns
                        WHERE session_id = :session_id AND concept_id IS NOT NULL
                   )
               AND cm.learner_subject_id = (
                       SELECT learner_subject_id
                         FROM learning_sessions
                        WHERE id = :session_id
                   )
            """
        ),
        {
            "session_id": session_id,
            "half_life_seconds": DECAY_HALF_LIFE.total_seconds(),
        },
    )
    return int(result.rowcount or 0)


async def _update_focus(context: SessionContext, agents: Any) -> bool:
    """§16 step 4: point the enrollment at the Curator's next topic."""
    output = await agents.curator.handle(
        AgentInput(session_context=context, kind="next_topic", payload={})
    )
    next_topic = output.structured
    concept_id = getattr(next_topic, "concept_id", None)
    if concept_id is None:
        return False

    def write(session: Session) -> None:
        session.execute(
            sql(
                """
                UPDATE learner_subjects
                   SET current_focus_concept_id = :concept_id
                 WHERE id = :learner_subject_id
                """
            ),
            {
                "concept_id": concept_id,
                "learner_subject_id": context.learner_subject_id,
            },
        )

    await run_db(write)
    return True


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str)
