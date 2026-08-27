"""When to offer office hours (agent runtime v1.0.1 §5.3).

A learner who raised their hand mid-lecture is in PAUSED_FOR_QUESTION. Most of
those resolve in a turn or two and the lecture resumes. Some do not -- the
question turns out to be a whole topic, and continuing to answer it inside a
paused lecture serves nobody. §5.1 puts a "Take this to office hours" action on
the resumption card once that has plainly happened.

**One computation, two callers.** §5.3 is explicit that the client uses this to
decide whether to render the button and the server uses it to verify the
escalate endpoint. Two implementations of "plainly happened" would drift, and
the drift would show up as a button that 400s when pressed. This module is the
one implementation; the client gets the answer from the session-state endpoint
rather than deriving its own.

**Defence in depth, not distrust.** The server check is not there because the
client is expected to misbehave. It is there because the threshold is
time-dependent: a card rendered at nine minutes is still on screen at eleven,
and a learner who leaves the tab open and comes back should not be refused for
pressing a button the page legitimately offered them. The server re-derives at
press time and, being time-based, will normally agree.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.asyncdb import read_db

#: §5.1: "3+ Tutor turns in this PAUSED_FOR_QUESTION span".
ESCALATION_TURN_THRESHOLD = 3

#: §5.1: "OR 10+ minutes since the original interrupt".
ESCALATION_ELAPSED = dt.timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class EscalationStatus:
    """Why the offer is or is not being made.

    Carries the counts rather than only the verdict so the client can render
    the reason, and so a support conversation about "why was I offered this"
    has an answer that is not "the model decided".
    """

    should_offer: bool
    tutor_turns: int
    minutes_since_interrupt: float
    reason: str

    def as_payload(self) -> dict[str, object]:
        return {
            "should_offer": self.should_offer,
            "tutor_turns": self.tutor_turns,
            "minutes_since_interrupt": round(self.minutes_since_interrupt, 1),
            "reason": self.reason,
        }


def _evaluate(
    session: Session, session_id: uuid.UUID, *, now: dt.datetime | None = None
) -> EscalationStatus:
    now = now or dt.datetime.now(dt.UTC)

    # The interruption that opened this paused span is the most recent learner
    # turn whose recorded intent was `interrupt`. Anchoring on the turn rather
    # than on an in-memory timestamp means a rebuilt Orchestrator (§7) computes
    # the same answer as the one that served the interrupt.
    row = session.execute(
        sql(
            """
            SELECT turn_index, created_at
              FROM session_turns
             WHERE session_id = :session_id
               AND actor = 'learner'
               AND input ->> 'intent' = 'interrupt'
             ORDER BY turn_index DESC
             LIMIT 1
            """
        ),
        {"session_id": session_id},
    ).first()

    if row is None:
        return EscalationStatus(
            should_offer=False,
            tutor_turns=0,
            minutes_since_interrupt=0.0,
            reason="no interruption in this session",
        )

    tutor_turns = int(
        session.execute(
            sql(
                """
                SELECT count(*)
                  FROM session_turns
                 WHERE session_id = :session_id
                   AND actor = 'tutor'
                   AND turn_index > :since
                """
            ),
            {"session_id": session_id, "since": row.turn_index},
        ).scalar_one()
    )

    started = row.created_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=dt.UTC)
    minutes = (now - started).total_seconds() / 60

    if tutor_turns >= ESCALATION_TURN_THRESHOLD:
        return EscalationStatus(
            True, tutor_turns, minutes,
            f"{tutor_turns} tutor turns without resuming the lecture",
        )
    if minutes >= ESCALATION_ELAPSED.total_seconds() / 60:
        return EscalationStatus(
            True, tutor_turns, minutes,
            f"{minutes:.0f} minutes paused without resuming the lecture",
        )
    return EscalationStatus(
        False, tutor_turns, minutes,
        f"{tutor_turns} tutor turns, {minutes:.0f} minutes -- below both thresholds",
    )


async def escalation_status(
    session_id: uuid.UUID, *, now: dt.datetime | None = None
) -> EscalationStatus:
    """Full status, for the client to render and explain."""
    return await read_db(lambda s: _evaluate(s, session_id, now=now))


async def should_offer_escalation(
    session_id: uuid.UUID, *, now: dt.datetime | None = None
) -> bool:
    """§5.3's named predicate. The verdict alone, for the server-side check."""
    return (await escalation_status(session_id, now=now)).should_offer
