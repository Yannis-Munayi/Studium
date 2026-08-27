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

**Every transition names three things: source, destination, and the emission
path of its triggering event** (v1.0.1 §2). A table that names only source and
destination is a diagram, not a specification -- in code every event is emitted
by *something*, and a spec that omits the emitter produces a machine that looks
complete while several of its transitions are unreachable. That is not a
hypothetical: R14 was exactly this defect, and it left every real session
sitting in ``OPENING`` for its entire life while four separate test tiers
agreed the machine was fine.

So :class:`Transition` carries an :class:`Emission`, and it is not
documentation. ``tests/orchestration/test_emission_paths.py`` walks this table
and fails if a client emission names an endpoint the FastAPI router does not
serve, or an internal emission names a function that does not exist. Adding a
row without wiring its emitter fails at commit time.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, replace
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
    #: Where the machine is as the guard runs. Set by :meth:`peek`, never by a
    #: caller -- the primitive matrix guard needs the source state, and asking
    #: every call site to pass it correctly is how it comes to be passed wrong.
    source_state: State | None = None
    #: Rubric criteria still unanswered in a summative attempt (evaluation
    #: §11.2). Distinct from ``segments_remaining``: a lecture's segments are
    #: generated as it goes, while an attempt's criteria are fixed when it
    #: starts, and the difference is what makes a summative attempt finite.
    criteria_remaining: int = 0


class EmissionKind(StrEnum):
    """How a transition's triggering event reaches the machine (v1.0.1 §3)."""

    #: An HTTP request from the frontend. ``path`` names the endpoint.
    CLIENT = "client"
    #: An Orchestrator or agent function completing. ``path`` names it.
    INTERNAL = "internal"
    #: A background signal -- timer, stream boundary, budget cap. ``path``
    #: names the signal's origin and its trigger condition.
    AMBIENT = "ambient"


@dataclass(frozen=True, slots=True)
class Emission:
    """Who fires this transition's event, and from where.

    ``path`` is checked by the emission-path test, so it must be literal
    enough to resolve: ``"POST /api/session/{session_id}/resume"`` for a client
    emission, a dotted ``module.function`` for an internal one. Prose is only
    acceptable on :attr:`EmissionKind.AMBIENT`, where the trigger is a
    condition rather than a call site -- and even there the handler is named.
    """

    kind: EmissionKind
    path: str

    def __str__(self) -> str:
        return f"{self.kind.value}: {self.path}"


def client(path: str) -> Emission:
    return Emission(EmissionKind.CLIENT, path)


def internal(path: str) -> Emission:
    return Emission(EmissionKind.INTERNAL, path)


def ambient(path: str) -> Emission:
    return Emission(EmissionKind.AMBIENT, path)


@dataclass(frozen=True, slots=True)
class PrimitiveRule:
    """One row of v1.0.1 §3.2's primitive validity matrix."""

    valid_from: frozenset[State]
    #: Where the session lands. ``None`` means it stays in the source state.
    destination: State | None = None
    note: str = ""


#: The states a primitive palette is offered from. v1.0.1 §3.2 lists
#: LECTURING, TUTORIAL, LAB and OFFICE_HOURS.
#:
#: PAUSED_FOR_QUESTION is added to every row that already contains TUTORIAL,
#: and is *not* in the patch's matrix. R13 established it deliberately --
#: raising a hand and then asking for a different explanation is one gesture,
#: not a state that forbids it -- and dropping it here would silently re-break
#: what R13 fixed. Recorded as R17.
_PAUSED = State.PAUSED_FOR_QUESTION

PRIMITIVE_MATRIX: dict[str, PrimitiveRule] = {
    "explain_differently": PrimitiveRule(
        frozenset({State.LECTURING, State.TUTORIAL, State.OFFICE_HOURS, _PAUSED}),
        None,
        "Tutor diagnoses, Lecturer regenerates in a new stance; the session "
        "does not leave the state it was explaining from.",
    ),
    "prove_it_to_me": PrimitiveRule(
        frozenset({State.LECTURING, State.TUTORIAL, State.OFFICE_HOURS, _PAUSED}),
        State.TUTORIAL,
        "The one hold-state primitive that moves: eliciting a derivation is a "
        "tutorial exchange, so a lecture becomes one. Returning to the lecture "
        "goes through question_resolved, per the interrupt flow.",
    ),
    "where_does_this_fit": PrimitiveRule(
        frozenset(
            {State.LECTURING, State.TUTORIAL, State.LAB, State.OFFICE_HOURS, _PAUSED}
        )
    ),
    "vocabulary_check": PrimitiveRule(
        frozenset(
            {State.LECTURING, State.TUTORIAL, State.LAB, State.OFFICE_HOURS, _PAUSED}
        )
    ),
    "show_worked_example": PrimitiveRule(
        frozenset({State.LECTURING, State.TUTORIAL, State.LAB, _PAUSED}),
        None,
        "Not offered in OFFICE_HOURS per §3.2.",
    ),
    "let_me_try_one": PrimitiveRule(
        frozenset({State.LECTURING, State.TUTORIAL, _PAUSED}),
        State.LAB,
        "Invalid from LAB: the learner already has a problem in front of them.",
    ),
    "why_does_this_matter": PrimitiveRule(
        frozenset(
            {State.LECTURING, State.TUTORIAL, State.LAB, State.OFFICE_HOURS, _PAUSED}
        )
    ),
    "im_lost": PrimitiveRule(
        frozenset(
            {State.LECTURING, State.TUTORIAL, State.LAB, State.OFFICE_HOURS, _PAUSED}
        ),
        None,
        "Holds state here. The Curator may reset the focus concept afterwards, "
        "which is a context change rather than a transition.",
    ),
}

#: Every state some primitive can be invoked from -- the set needing a
#: PRIMITIVE_INVOKED row in the table.
PRIMITIVE_SOURCE_STATES: frozenset[State] = frozenset(
    state for rule in PRIMITIVE_MATRIX.values() for state in rule.valid_from
)


class InvalidPrimitive(ValueError):
    """A primitive invoked from a state §3.2 does not allow.

    §3.2: "Silent no-op is not acceptable -- the failure must be surfaced to
    the client so the UI can present a clear message." The API layer turns this
    into a 400 naming the combination.
    """

    def __init__(self, primitive: str, state: State) -> None:
        self.primitive = primitive
        self.state = state
        known = primitive in PRIMITIVE_MATRIX
        if known:
            allowed = ", ".join(sorted(s.value for s in PRIMITIVE_MATRIX[primitive].valid_from))
            detail = f"it is available from: {allowed}"
        else:
            detail = f"no such primitive; expected one of {sorted(PRIMITIVE_MATRIX)}"
        super().__init__(f"{primitive!r} cannot be invoked from {state.value} -- {detail}")


def primitive_is_valid(primitive: str | None, state: State | None) -> bool:
    rule = PRIMITIVE_MATRIX.get(primitive or "")
    return bool(rule and state in rule.valid_from)


def primitive_destination(primitive: str | None, source: State) -> State:
    """Where ``primitive`` leaves the session, given where it started."""
    rule = PRIMITIVE_MATRIX.get(primitive or "")
    if rule is None:
        raise InvalidPrimitive(primitive or "<none>", source)
    return rule.destination or source


@dataclass(frozen=True, slots=True)
class Transition:
    """One row of §7's transition table."""

    source: State | None  # None matches any state ("any" in the table)
    event: Event
    target: State | None  # None means resolved from the guard context
    guard: str = "always"
    effect: str = ""
    note: str = ""
    #: Who fires the event (v1.0.1 §3). A tuple because some events have
    #: genuinely several emitters -- ``end_session`` arrives from the close
    #: endpoint, the idle timer, or a budget cap, and naming only the first
    #: would leave the other two exactly as unexamined as R14's missing one.
    #:
    #: Defaulted empty so a row under construction fails the emission-path
    #: test, which says what is wrong, rather than a TypeError here, which
    #: says only where.
    emissions: tuple[Emission, ...] = ()


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
    if transition.effect == "primitive_destination":
        # v1.0.1 §3.2: destination is per-primitive, not per-source-state. Six
        # of the eight hold the state they were invoked from; let_me_try_one
        # goes to LAB and prove_it_to_me to TUTORIAL.
        source = transition.source or guards.source_state or State.TUTORIAL
        return primitive_destination(guards.primitive, source)
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
    # v1.0.1 §3.2 replaces the old let_me_try_one/everything-else split with a
    # per-primitive matrix, so there is one guard and it asks the matrix. The
    # destination is resolved separately by _resolve_dynamic; this only decides
    # whether the combination is legal at all.
    "primitive_valid_here": lambda g: primitive_is_valid(g.primitive, g.source_state),
    "answer_correct": lambda g: g.evaluator_correct is True,
    "answer_incorrect_within_attempts": lambda g: (
        g.evaluator_correct is False and g.failed_attempts < g.max_attempts
    ),
    "answer_incorrect_attempts_exhausted": lambda g: (
        g.evaluator_correct is False and g.failed_attempts >= g.max_attempts
    ),
    # Evaluation §11.2. Deliberately *not* keyed on the verdict: a summative
    # attempt is single-submission, so right and wrong go the same way. A
    # correct/incorrect split here would be the retry loop the closed-book rule
    # exists to remove.
    "criteria_remain": lambda g: g.criteria_remaining > 0,
    "criteria_exhausted": lambda g: g.criteria_remaining <= 0,
}

#: §7's transition table, in evaluation order. First match wins, so more
#: specific guards precede more permissive ones on the same (state, event).
TRANSITIONS: tuple[Transition, ...] = (
    Transition(
        State.IDLE, Event.START_SESSION, State.OPENING, "budget_ok", "create_session",
        emissions=(client("POST /api/session"),),
    ),
    # OPENING splits on whether there is prior context to check retention against.
    # v1.0.1 §3.1 pins the emitter: start_session, once the context is assembled
    # and the retrieval check has either run or been skipped. Before the patch
    # nothing fired this at all and every session stayed here (R14).
    Transition(
        State.OPENING, Event.CONTEXT_READY, None, "has_prior_summary",
        "run_retrieval_check", "retrieval-check subroutine, then the mode's state",
        emissions=(internal("studium.agents.orchestrator.Orchestrator.start_session"),),
    ),
    Transition(
        State.OPENING, Event.CONTEXT_READY, None, "no_prior_summary", "enter_mode_state",
        emissions=(internal("studium.agents.orchestrator.Orchestrator.start_session"),),
    ),
    Transition(
        State.LECTURING, Event.SEGMENT_COMPLETE, State.LECTURING,
        "segments_remain", "next_segment",
        emissions=(internal("studium.agents.orchestrator.Orchestrator._handle_conversational"),),
    ),
    Transition(
        State.LECTURING, Event.SEGMENT_COMPLETE, None,
        "segments_exhausted", "curator_chooses_next",
        emissions=(internal("studium.agents.orchestrator.Orchestrator._handle_conversational"),),
    ),
    Transition(
        State.LECTURING, Event.LEARNER_INTERRUPT, State.INTERRUPTED,
        "stream_in_progress", "signal_finish_sentence",
        emissions=(client("POST /api/session/{session_id}/interrupt"),),
    ),
    Transition(
        State.INTERRUPTED, Event.SENTENCE_BOUNDARY_REACHED, State.PAUSED_FOR_QUESTION,
        "always", "cancel_remaining_tokens",
        emissions=(internal("studium.orchestration.streaming.sentence_boundary_iter"),),
    ),
    Transition(
        State.PAUSED_FOR_QUESTION, Event.QUESTION_RESOLVED, None,
        "learner_satisfied", "resume_prior_state",
        emissions=(client("POST /api/session/{session_id}/resume"),),
    ),
    Transition(
        State.PAUSED_FOR_QUESTION, Event.ESCALATE, State.OFFICE_HOURS,
        emissions=(client("POST /api/session/{session_id}/escalate"),),
    ),
    # One PRIMITIVE_INVOKED row per source state the matrix allows. Both the
    # guard and the destination consult PRIMITIVE_MATRIX, so §3.2 is the single
    # source of truth and this table does not restate it -- adding a primitive
    # or widening its valid_from needs no change here.
    *(
        Transition(
            state, Event.PRIMITIVE_INVOKED, None,
            "primitive_valid_here", "primitive_destination",
            emissions=(client("POST /api/session/{session_id}/primitive"),),
        )
        for state in sorted(PRIMITIVE_SOURCE_STATES, key=lambda s: s.value)
    ),
    Transition(
        State.LAB, Event.ANSWER_SUBMITTED, None, "answer_correct",
        "curator_chooses_after_correct",
        emissions=(client("POST /api/session/{session_id}/practice/submit"),),
    ),
    Transition(
        State.LAB, Event.ANSWER_SUBMITTED, State.LAB,
        "answer_incorrect_within_attempts", "record_incorrect_attempt",
        note="Wrong but attempts remain: stay in LAB and let them try again.",
        emissions=(client("POST /api/session/{session_id}/practice/submit"),),
    ),
    Transition(
        State.LAB, Event.ANSWER_SUBMITTED, State.TUTORIAL,
        "answer_incorrect_attempts_exhausted", "tutor_takes_over",
        emissions=(client("POST /api/session/{session_id}/practice/submit"),),
    ),
    # v1.0.1 §3.1 gives REVIEW the same submit endpoint as LAB: a due card is
    # answered the same way a practice problem is, and the Evaluator's
    # check_partial grades both.
    Transition(
        State.REVIEW, Event.ANSWER_SUBMITTED, State.REVIEW,
        "always", "grade_review_card",
        note="Stays in REVIEW while cards remain; the deck emptying is what "
        "ends the session, not this transition.",
        emissions=(client("POST /api/session/{session_id}/practice/submit"),),
    ),
    # An interrupt during a tutorial exchange pauses it the same way a lecture
    # interrupt does. §7 draws the arrow into PAUSED_FOR_QUESTION from TUTORIAL
    # in the diagram but omits the row from the table. See DIVERGENCES (R7).
    Transition(
        State.TUTORIAL, Event.LEARNER_INTERRUPT, State.PAUSED_FOR_QUESTION,
        "stream_in_progress", "signal_finish_sentence",
        emissions=(client("POST /api/session/{session_id}/interrupt"),),
    ),
    # Evaluation §11.2's summative flow. Before these two rows the state was
    # reachable and inescapable: MODE_ENTRY_STATE puts a session in
    # SUMMATIVE_ASSESSMENT and no transition led out of it except the wildcard
    # END_SESSION below, so a learner who submitted an answer got
    # IllegalTransition. Nothing caught it because no test opened a session in
    # that mode. See DIVERGENCES-EVALUATION (E10).
    #
    # One row per exhaustion branch and *no verdict branch*: §11.2 is
    # single-submission, so a correct answer and a wrong one both advance. The
    # three-row shape LAB uses (correct / retry / exhausted) is precisely the
    # multi-attempt cycle closed-book assessment replaces.
    Transition(
        State.SUMMATIVE_ASSESSMENT, Event.ANSWER_SUBMITTED, State.SUMMATIVE_ASSESSMENT,
        "criteria_remain", "record_assessment_response",
        note="Answer recorded, no verdict shown, no revision. The next "
        "criterion is presented; §11.4 releases scores only after all "
        "criteria are submitted.",
        emissions=(client("POST /api/session/{session_id}/practice/submit"),),
    ),
    Transition(
        State.SUMMATIVE_ASSESSMENT, Event.ANSWER_SUBMITTED, State.CLOSING,
        "criteria_exhausted", "grade_assessment_attempt",
        note="Last criterion submitted: the Evaluator grades the whole attempt "
        "(§11.4) and the session closes. Grading is not a state -- there is no "
        "event that would leave one, so the session would strand there.",
        emissions=(client("POST /api/session/{session_id}/practice/submit"),),
    ),
    # §3.3's two ambient signals converge on this same event, so the row names
    # all three emitters rather than only the one a reader would think of.
    Transition(
        None, Event.END_SESSION, State.CLOSING, "always", "begin_close",
        emissions=(
            client("POST /api/session/{session_id}/close"),
            ambient(
                "studium.orchestration.state_machine.timed_out -- no turn for "
                "target_duration_minutes + 15"
            ),
            ambient(
                "studium.session.budget_gate.pre_flight_check -- raises "
                "BudgetExceededError when a hard cap would be crossed"
            ),
        ),
    ),
    Transition(
        State.CLOSING, Event.CLOSE_COMPLETE, State.IDLE, "always", "finish_close",
        emissions=(internal("studium.session.lifecycle.close_session"),),
    ),
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
        # The primitive matrix guard needs to know where the machine is, and
        # the machine is the only thing that reliably does. Stamped here rather
        # than asked of every caller: a source state a caller passes is a
        # source state a caller can pass wrongly.
        g = replace(g, source_state=self.state)
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
