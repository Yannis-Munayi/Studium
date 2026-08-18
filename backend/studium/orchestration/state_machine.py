"""The session state machine (agent runtime §7).

One machine per active session, owned by the Orchestrator. Kept deliberately
pure -- no database, no model calls, no clock beyond what is passed in -- so
the offline tier can enumerate every ``(state, event)`` pair and assert the
result against the transition table, which is the §23 Tier 1 requirement.

Guards that the spec states in prose ("Segments exhausted", "Evaluator:
incorrect after N attempts") are boolean fields on :class:`GuardContext`.
Making them data rather than closures is what lets the test enumerate them:
a guard hidden inside a lambda over live session state cannot be swept.

**State is not persisted.** §7 is explicit that a ``current_state`` column
would create a synchronisation bug surface. It lives on the in-memory
Orchestrator and, if the process restarts, is rebuilt from the ``session_turns``
tail by :func:`reconstruct_state`.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class State(StrEnum):
    IDLE = "IDLE"
    OPENING = "OPENING"
    LECTURING = "LECTURING"
    TUTORIAL = "TUTORIAL"
    LAB = "LAB"
    REVIEW = "REVIEW"
    INTERRUPTED = "INTERRUPTED"
    PAUSED_FOR_QUESTION = "PAUSED_FOR_QUESTION"
    OFFICE_HOURS = "OFFICE_HOURS"
    SUMMATIVE_ASSESSMENT = "SUMMATIVE_ASSESSMENT"
    CLOSING = "CLOSING"


class Event(StrEnum):
    START_SESSION = "start_session"
    CONTEXT_READY = "context_ready"
    SEGMENT_COMPLETE = "segment_complete"
    LEARNER_INTERRUPT = "learner_interrupt"
    SENTENCE_BOUNDARY_REACHED = "sentence_boundary_reached"
    QUESTION_RESOLVED = "question_resolved"
    ESCALATE = "escalate"
    PRIMITIVE_INVOKED = "primitive_invoked"
    ANSWER_SUBMITTED = "answer_submitted"
    END_SESSION = "end_session"
    CLOSE_COMPLETE = "close_complete"


#: State -> the persisted ``session_mode`` it corresponds to (data layer §6.0).
#: ``None`` means "unchanged from prior" -- INTERRUPTED and PAUSED_FOR_QUESTION
#: are runtime-only refinements of whatever mode the session is already in.
PERSISTED_MODE: dict[State, str | None] = {
    State.IDLE: None,
    State.OPENING: None,
    State.LECTURING: "lecture",
    State.TUTORIAL: "tutorial",
    State.LAB: "lab",
    State.REVIEW: "review",
    State.INTERRUPTED: None,
    State.PAUSED_FOR_QUESTION: None,
    State.OFFICE_HOURS: "office_hours",
    State.SUMMATIVE_ASSESSMENT: "summative_assessment",
    State.CLOSING: None,
}

#: session_mode -> the state a session in that mode opens into.
MODE_ENTRY_STATE: dict[str, State] = {
    "lecture": State.LECTURING,
    "tutorial": State.TUTORIAL,
    "lab": State.LAB,
    "review": State.REVIEW,
    "office_hours": State.OFFICE_HOURS,
    "summative_assessment": State.SUMMATIVE_ASSESSMENT,
    # Orientation has no dedicated runtime state; it opens as a tutorial, which
    # is the shape an intake conversation actually takes.
    "orientation": State.TUTORIAL,
}

#: §7 "Timeouts": idle longer than this past the target duration and the
#: Orchestrator ends the session rather than letting it hold budget forever.
IDLE_TIMEOUT_GRACE_MINUTES = 15


class IllegalTransition(RuntimeError):
    """No transition matches this (state, event, guard) triple."""

    def __init__(self, state: State, event: Event) -> None:
        self.state = state
        self.event = event
        super().__init__(f"no transition from {state} on {event}")


@dataclass(slots=True)
class GuardContext:
    """Guard inputs, as data rather than closures.

    Defaults describe the common case, so a test that cares about one guard
    sets one field.
    """

    budget_ok: bool = True
    has_prior_summary: bool = False
    segments_remaining: int = 0
    stream_in_progress: bool = False
    learner_satisfied: bool = True
    primitive: str | None = None
    evaluator_correct: bool | None = None
    failed_attempts: int = 0
    max_attempts: int = 2
    #: The Curator's choice of what comes after a finished lecture or a correct
    #: lab answer. §7 leaves both as "TUTORIAL or LAB per Curator".
    curator_next_state: State = State.TUTORIAL
    #: The mode the session was started in, for OPENING's exit.
    session_mode: str = "lecture"
    #: State to resume after an interruption is resolved.
    resume_state: State = State.LECTURING


@dataclass(frozen=True, slots=True)
class Transition:
    """One row of §7's transition table."""

    source: State | None  # None matches any state ("any" in the table)
    event: Event
    target: State | None  # None means resolved from the guard context
    guard: str = "always"
    effect: str = ""
    note: str = ""


def _resolve_dynamic(transition: Transition, guards: GuardContext) -> State:
    """Targets the table states in prose rather than by name."""
    if transition.effect in {"enter_mode_state", "run_retrieval_check"}:
        # §7 gives the prior-summary branch the target "(retrieval-check
        # subroutine)". The check is work done *during* the transition, not a
        # state the session rests in -- there is no event that would leave it,
        # so modelling it as a state would strand the session. It runs as the
        # transition's effect and the destination is the mode's state either
        # way, which is what the diagram's arrows show. See DIVERGENCES (R12).
        return MODE_ENTRY_STATE.get(guards.session_mode, State.TUTORIAL)
    if transition.effect in {"curator_chooses_next", "curator_chooses_after_correct"}:
        return guards.curator_next_state
    if transition.effect == "resume_prior_state":
        return guards.resume_state
    raise IllegalTransition(transition.source or State.IDLE, transition.event)


#: Guard predicates, by name. Pure functions of GuardContext.
GUARDS = {
    "always": lambda g: True,
    "budget_ok": lambda g: g.budget_ok,
    "has_prior_summary": lambda g: g.has_prior_summary,
    "no_prior_summary": lambda g: not g.has_prior_summary,
    "segments_remain": lambda g: g.segments_remaining > 0,
    "segments_exhausted": lambda g: g.segments_remaining <= 0,
    "stream_in_progress": lambda g: g.stream_in_progress,
    "learner_satisfied": lambda g: g.learner_satisfied,
    "primitive_is_let_me_try_one": lambda g: g.primitive == "let_me_try_one",
    "primitive_stays_in_tutorial": lambda g: g.primitive != "let_me_try_one",
    "answer_correct": lambda g: g.evaluator_correct is True,
    "answer_incorrect_within_attempts": lambda g: (
        g.evaluator_correct is False and g.failed_attempts < g.max_attempts
    ),
    "answer_incorrect_attempts_exhausted": lambda g: (
        g.evaluator_correct is False and g.failed_attempts >= g.max_attempts
    ),
}

#: §7's transition table, in evaluation order. First match wins, so more
#: specific guards precede more permissive ones on the same (state, event).
TRANSITIONS: tuple[Transition, ...] = (
    Transition(State.IDLE, Event.START_SESSION, State.OPENING, "budget_ok", "create_session"),
    # OPENING splits on whether there is prior context to check retention against.
    Transition(
        State.OPENING, Event.CONTEXT_READY, None, "has_prior_summary",
        "run_retrieval_check", "retrieval-check subroutine, then the mode's state",
    ),
    Transition(
        State.OPENING, Event.CONTEXT_READY, None, "no_prior_summary", "enter_mode_state"
    ),
    Transition(
        State.LECTURING, Event.SEGMENT_COMPLETE, State.LECTURING,
        "segments_remain", "next_segment",
    ),
    Transition(
        State.LECTURING, Event.SEGMENT_COMPLETE, None,
        "segments_exhausted", "curator_chooses_next",
    ),
    Transition(
        State.LECTURING, Event.LEARNER_INTERRUPT, State.INTERRUPTED,
        "stream_in_progress", "signal_finish_sentence",
    ),
    Transition(
        State.INTERRUPTED, Event.SENTENCE_BOUNDARY_REACHED, State.PAUSED_FOR_QUESTION,
        "always", "cancel_remaining_tokens",
    ),
    Transition(
        State.PAUSED_FOR_QUESTION, Event.QUESTION_RESOLVED, None,
        "learner_satisfied", "resume_prior_state",
    ),
    Transition(State.PAUSED_FOR_QUESTION, Event.ESCALATE, State.OFFICE_HOURS),
    Transition(
        State.TUTORIAL, Event.PRIMITIVE_INVOKED, State.LAB, "primitive_is_let_me_try_one"
    ),
    Transition(
        State.TUTORIAL, Event.PRIMITIVE_INVOKED, State.TUTORIAL,
        "primitive_stays_in_tutorial",
        note="Seven of the eight primitives resolve inside the tutorial; only "
        "let_me_try_one changes state.",
    ),
    Transition(
        State.LAB, Event.ANSWER_SUBMITTED, None, "answer_correct",
        "curator_chooses_after_correct",
    ),
    Transition(
        State.LAB, Event.ANSWER_SUBMITTED, State.LAB,
        "answer_incorrect_within_attempts", "record_incorrect_attempt",
        note="Wrong but attempts remain: stay in LAB and let them try again.",
    ),
    Transition(
        State.LAB, Event.ANSWER_SUBMITTED, State.TUTORIAL,
        "answer_incorrect_attempts_exhausted", "tutor_takes_over",
    ),
    # An interrupt during a tutorial exchange pauses it the same way a lecture
    # interrupt does. §7 draws the arrow into PAUSED_FOR_QUESTION from TUTORIAL
    # in the diagram but omits the row from the table. See DIVERGENCES (R7).
    Transition(
        State.TUTORIAL, Event.LEARNER_INTERRUPT, State.PAUSED_FOR_QUESTION,
        "stream_in_progress", "signal_finish_sentence",
    ),
    Transition(None, Event.END_SESSION, State.CLOSING, "always", "begin_close"),
    Transition(State.CLOSING, Event.CLOSE_COMPLETE, State.IDLE, "always", "finish_close"),
)


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    """One applied transition, for the in-memory log (§7)."""

    source: State
    event: Event
    target: State
    effect: str
    at: dt.datetime


class SessionStateMachine:
    """The per-session machine.

    The transition log is in-memory and bounded: §7 says the persistent record
    is the ``session_turns`` sequence, so this exists for debugging a live
    session, not for audit.
    """

    def __init__(self, state: State = State.IDLE, *, log_size: int = 64) -> None:
        self.state = state
        self.log: deque[TransitionRecord] = deque(maxlen=log_size)
        #: What LECTURING/TUTORIAL to return to after PAUSED_FOR_QUESTION.
        self.resume_state: State = State.LECTURING
        #: Where an interrupted stream was cut, so the Lecturer can resume.
        self.interruption_point: dict[str, Any] = {}

    def can(self, event: Event, guards: GuardContext | None = None) -> bool:
        try:
            self.peek(event, guards)
        except IllegalTransition:
            return False
        return True

    def peek(self, event: Event, guards: GuardContext | None = None) -> tuple[State, str]:
        """Resolve the target without applying it."""
        g = guards or GuardContext(resume_state=self.resume_state)
        for transition in TRANSITIONS:
            if transition.event is not event:
                continue
            if transition.source is not None and transition.source is not self.state:
                continue
            # "any -> CLOSING on end_session" must not fire from CLOSING itself.
            if transition.source is None and self.state is State.CLOSING:
                continue
            if not GUARDS[transition.guard](g):
                continue
            target = transition.target or _resolve_dynamic(transition, g)
            return target, transition.effect
        raise IllegalTransition(self.state, event)

    def fire(
        self, event: Event, guards: GuardContext | None = None, *, now: dt.datetime | None = None
    ) -> tuple[State, str]:
        """Apply a transition. Returns ``(new_state, effect_name)``."""
        g = guards or GuardContext(resume_state=self.resume_state)
        source = self.state
        target, effect = self.peek(event, g)

        # Remember where to come back to before leaving an interruptible state.
        if event is Event.LEARNER_INTERRUPT:
            self.resume_state = source if source is not State.INTERRUPTED else self.resume_state

        self.state = target
        self.log.append(
            TransitionRecord(
                source=source,
                event=event,
                target=target,
                effect=effect,
                at=now or dt.datetime.now(dt.UTC),
            )
        )
        return target, effect

    @property
    def persisted_mode(self) -> str | None:
        return PERSISTED_MODE[self.state]

    @property
    def is_interruptible(self) -> bool:
        """Whether an interrupt is meaningful right now.

        Only states that stream learner-visible output can be interrupted;
        an interrupt arriving in LAB or CLOSING is a no-op, not an error.
        """
        return self.state in {State.LECTURING, State.TUTORIAL}


def timed_out(
    *,
    started_at: dt.datetime,
    last_turn_at: dt.datetime | None,
    target_duration_minutes: int,
    now: dt.datetime | None = None,
) -> bool:
    """§7's idle timeout: no active turn past target duration + grace.

    Measured from the last turn rather than from session start, so a learner
    working steadily past their target is not cut off mid-thought -- only an
    abandoned session is.
    """
    now = now or dt.datetime.now(dt.UTC)
    reference = last_turn_at or started_at
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=dt.UTC)
    idle_minutes = (now - reference).total_seconds() / 60
    return idle_minutes > target_duration_minutes + IDLE_TIMEOUT_GRACE_MINUTES


def reconstruct_state(turns: Sequence[dict[str, Any]], *, mode: str) -> State:
    """Rebuild the machine's state from the ``session_turns`` tail (§7).

    Used when the process restarts mid-session. The reconstruction is
    deliberately coarse -- it recovers the mode-level state, not transient
    INTERRUPTED / PAUSED_FOR_QUESTION, because an interruption whose stream
    died with the process has no stream left to resume. The learner gets the
    mode they were in and the Lecturer picks up from the last delivered
    segment, which is the honest recovery.
    """
    if not turns:
        return MODE_ENTRY_STATE.get(mode, State.TUTORIAL)

    last = turns[-1]
    actor = str(last.get("actor", ""))
    if actor == "lecturer":
        return State.LECTURING
    if actor == "tutor":
        return State.TUTORIAL if mode != "office_hours" else State.OFFICE_HOURS
    if actor == "reviewer":
        return State.REVIEW
    if actor == "evaluator":
        return State.LAB if mode == "lab" else MODE_ENTRY_STATE.get(mode, State.TUTORIAL)
    return MODE_ENTRY_STATE.get(mode, State.TUTORIAL)
