"""The session state machine (agent runtime §23 Tier 1, §7).

§23: "State machine transitions: enumerate all (state, event) pairs; assert the
resulting state matches the transition table."

The sweep below is exhaustive over ``State x Event`` -- 121 pairs -- and asserts
that each either produces the documented target or raises
:class:`IllegalTransition`. An exhaustive sweep is the only kind worth writing
here: the interesting bugs in a state machine are the transitions nobody thought
to write down, and a sample cannot find those.
"""

from __future__ import annotations

import datetime as dt

import pytest

from studium.orchestration.state_machine import (
    GUARDS,
    MODE_ENTRY_STATE,
    PERSISTED_MODE,
    PRIMITIVE_MATRIX,
    TRANSITIONS,
    Event,
    GuardContext,
    IllegalTransition,
    SessionStateMachine,
    State,
    reconstruct_state,
    timed_out,
)


class TestExhaustiveSweep:
    @pytest.mark.parametrize("state", list(State))
    @pytest.mark.parametrize("event", list(Event))
    def test_every_pair_either_transitions_or_refuses(self, state, event):
        """No (state, event) pair may crash, hang, or land somewhere undeclared."""
        machine = SessionStateMachine(state)
        try:
            target, _ = machine.fire(event, GuardContext())
        except IllegalTransition:
            return
        assert isinstance(target, State)
        assert machine.state is target

    def test_the_sweep_actually_exercises_both_outcomes(self):
        """Coverage guard: a sweep where everything refused would pass vacuously.

        Without this, deleting the whole transition table would leave the
        parametrised test green -- every pair would raise IllegalTransition and
        every assertion would be skipped by the early return.
        """
        allowed = refused = 0
        for state in State:
            for event in Event:
                try:
                    SessionStateMachine(state).fire(event, GuardContext())
                except IllegalTransition:
                    refused += 1
                else:
                    allowed += 1
        assert allowed >= 15, f"only {allowed} legal transitions; table looks truncated"
        assert refused > allowed, "expected most pairs to be illegal"

    def test_every_declared_transition_is_reachable(self):
        """A row nobody can fire is a row that lies about the runtime."""
        for transition in TRANSITIONS:
            source = transition.source or State.LECTURING
            machine = SessionStateMachine(source)
            guards = _guards_satisfying(transition.guard)
            assert machine.can(transition.event, guards), (
                f"{source} --{transition.event}--> unreachable under guard "
                f"{transition.guard!r}"
            )

    def test_every_guard_name_in_the_table_exists(self):
        for transition in TRANSITIONS:
            assert transition.guard in GUARDS, transition.guard


def _guards_satisfying(guard: str) -> GuardContext:
    """The narrowest GuardContext that satisfies a named guard."""
    return {
        "always": GuardContext(),
        "budget_ok": GuardContext(budget_ok=True),
        "has_prior_summary": GuardContext(has_prior_summary=True),
        "no_prior_summary": GuardContext(has_prior_summary=False),
        "segments_remain": GuardContext(segments_remaining=3),
        "segments_exhausted": GuardContext(segments_remaining=0),
        "stream_in_progress": GuardContext(stream_in_progress=True),
        "learner_satisfied": GuardContext(learner_satisfied=True),
        "primitive_valid_here": GuardContext(primitive="where_does_this_fit"),
        "answer_correct": GuardContext(evaluator_correct=True),
        "answer_incorrect_within_attempts": GuardContext(
            evaluator_correct=False, failed_attempts=0, max_attempts=2
        ),
        "answer_incorrect_attempts_exhausted": GuardContext(
            evaluator_correct=False, failed_attempts=2, max_attempts=2
        ),
        # Evaluation §11.2's summative flow. Neither sets evaluator_correct: a
        # summative submission advances regardless of verdict, and setting one
        # here would assert the opposite of what the guards are for.
        "criteria_remain": GuardContext(criteria_remaining=3),
        "criteria_exhausted": GuardContext(criteria_remaining=0),
    }[guard]


class TestSpecifiedTransitions:
    """The §7 table, row by row."""

    def test_idle_to_opening_requires_budget(self):
        machine = SessionStateMachine(State.IDLE)
        assert machine.fire(Event.START_SESSION, GuardContext(budget_ok=True))[0] is State.OPENING

        blocked = SessionStateMachine(State.IDLE)
        with pytest.raises(IllegalTransition):
            blocked.fire(Event.START_SESSION, GuardContext(budget_ok=False))

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("lecture", State.LECTURING),
            ("tutorial", State.TUTORIAL),
            ("lab", State.LAB),
            ("review", State.REVIEW),
            ("office_hours", State.OFFICE_HOURS),
            ("summative_assessment", State.SUMMATIVE_ASSESSMENT),
            ("orientation", State.TUTORIAL),
        ],
    )
    def test_opening_enters_the_modes_state(self, mode, expected):
        machine = SessionStateMachine(State.OPENING)
        target, _ = machine.fire(
            Event.CONTEXT_READY, GuardContext(has_prior_summary=False, session_mode=mode)
        )
        assert target is expected

    def test_lecture_continues_while_segments_remain(self):
        machine = SessionStateMachine(State.LECTURING)
        target, effect = machine.fire(
            Event.SEGMENT_COMPLETE, GuardContext(segments_remaining=2)
        )
        assert (target, effect) == (State.LECTURING, "next_segment")

    def test_exhausted_lecture_hands_to_the_curators_choice(self):
        for choice in (State.TUTORIAL, State.LAB):
            machine = SessionStateMachine(State.LECTURING)
            target, _ = machine.fire(
                Event.SEGMENT_COMPLETE,
                GuardContext(segments_remaining=0, curator_next_state=choice),
            )
            assert target is choice

    def test_full_interruption_round_trip(self):
        """LECTURING -> INTERRUPTED -> PAUSED_FOR_QUESTION -> LECTURING (§7)."""
        machine = SessionStateMachine(State.LECTURING)
        assert machine.fire(
            Event.LEARNER_INTERRUPT, GuardContext(stream_in_progress=True)
        )[0] is State.INTERRUPTED
        assert machine.fire(Event.SENTENCE_BOUNDARY_REACHED)[0] is State.PAUSED_FOR_QUESTION
        assert machine.fire(
            Event.QUESTION_RESOLVED,
            GuardContext(learner_satisfied=True, resume_state=machine.resume_state),
        )[0] is State.LECTURING

    def test_interrupt_remembers_where_to_resume(self):
        """A tutorial interruption must resume the tutorial, not a lecture."""
        machine = SessionStateMachine(State.TUTORIAL)
        machine.fire(Event.LEARNER_INTERRUPT, GuardContext(stream_in_progress=True))
        assert machine.resume_state is State.TUTORIAL
        assert machine.fire(
            Event.QUESTION_RESOLVED,
            GuardContext(learner_satisfied=True, resume_state=machine.resume_state),
        )[0] is State.TUTORIAL

    def test_interrupt_without_a_live_stream_is_refused(self):
        machine = SessionStateMachine(State.LECTURING)
        with pytest.raises(IllegalTransition):
            machine.fire(Event.LEARNER_INTERRUPT, GuardContext(stream_in_progress=False))

    def test_escalation_goes_to_office_hours(self):
        machine = SessionStateMachine(State.PAUSED_FOR_QUESTION)
        assert machine.fire(Event.ESCALATE)[0] is State.OFFICE_HOURS

    def test_only_let_me_try_one_moves_to_lab(self):
        """§15: seven primitives resolve inside the tutorial."""
        machine = SessionStateMachine(State.TUTORIAL)
        assert machine.fire(
            Event.PRIMITIVE_INVOKED, GuardContext(primitive="let_me_try_one")
        )[0] is State.LAB

        for other in ("im_lost", "prove_it_to_me", "vocabulary_check"):
            m = SessionStateMachine(State.TUTORIAL)
            assert m.fire(Event.PRIMITIVE_INVOKED, GuardContext(primitive=other))[0] is State.TUTORIAL

    @pytest.mark.parametrize(
        "source", [State.TUTORIAL, State.LECTURING, State.PAUSED_FOR_QUESTION]
    )
    def test_let_me_try_one_reaches_the_lab_from_every_state_that_offers_it(self, source):
        """The palette is on the classroom, not only on the tutorial (R13).

        §7's table has PRIMITIVE_INVOKED rows for TUTORIAL alone. A lecture
        session sits in LECTURING, so gating there meant the primitive ran, the
        `end` chunk announced LAB, and the machine stayed put -- after which the
        learner's answer routed to the Tutor rather than the Evaluator.
        """
        machine = SessionStateMachine(source)
        assert machine.fire(
            Event.PRIMITIVE_INVOKED, GuardContext(primitive="let_me_try_one")
        )[0] is State.LAB

    @pytest.mark.parametrize(
        "source", [State.TUTORIAL, State.LECTURING, State.PAUSED_FOR_QUESTION]
    )
    def test_hold_state_primitives_leave_the_state_alone(self, source):
        """v1.0.1 §3.2 splits the eight rather than treating seven alike.

        The old shape here was "let_me_try_one moves, the other seven do not".
        The matrix makes it two that move: `prove_it_to_me` becomes a tutorial
        exchange even when raised mid-lecture, because eliciting a derivation
        is not something a lecture does.
        """
        for other in ("im_lost", "show_worked_example", "why_does_this_matter"):
            machine = SessionStateMachine(source)
            assert machine.fire(
                Event.PRIMITIVE_INVOKED, GuardContext(primitive=other)
            )[0] is source

    @pytest.mark.parametrize("source", [State.LECTURING, State.OFFICE_HOURS])
    def test_prove_it_to_me_converts_the_session_to_a_tutorial(self, source):
        """§3.2's second moving primitive."""
        machine = SessionStateMachine(source)
        assert machine.fire(
            Event.PRIMITIVE_INVOKED, GuardContext(primitive="prove_it_to_me")
        )[0] is State.TUTORIAL

    @pytest.mark.parametrize(
        ("primitive", "source"),
        [
            ("let_me_try_one", State.LAB),        # already has a problem
            ("show_worked_example", State.OFFICE_HOURS),
            ("explain_differently", State.LAB),
            ("where_does_this_fit", State.REVIEW),  # no palette during review
            ("let_me_try_one", State.SUMMATIVE_ASSESSMENT),
        ],
    )
    def test_invalid_combinations_are_refused_not_silently_ignored(
        self, primitive, source
    ):
        """§3.2: "Silent no-op is not acceptable."

        A refused transition is what lets the API return a 400 naming the
        combination, instead of the client believing a primitive ran.
        """
        machine = SessionStateMachine(source)
        with pytest.raises(IllegalTransition):
            machine.fire(Event.PRIMITIVE_INVOKED, GuardContext(primitive=primitive))

    def test_an_unknown_primitive_never_transitions(self):
        machine = SessionStateMachine(State.TUTORIAL)
        with pytest.raises(IllegalTransition):
            machine.fire(Event.PRIMITIVE_INVOKED, GuardContext(primitive="teleport"))

    def test_the_matrix_is_the_only_source_of_truth(self):
        """Every table row's validity comes from PRIMITIVE_MATRIX, not the row.

        If a row hard-coded its own destination the two could disagree, and the
        disagreement would show up as a client and a runtime believing
        different states -- which is R13 all over again.
        """
        for primitive, rule in PRIMITIVE_MATRIX.items():
            for state in rule.valid_from:
                machine = SessionStateMachine(state)
                target, _ = machine.fire(
                    Event.PRIMITIVE_INVOKED, GuardContext(primitive=primitive)
                )
                assert target is (rule.destination or state), (primitive, state)

    def test_lab_answer_routes_on_the_verdict(self):
        correct = SessionStateMachine(State.LAB)
        assert correct.fire(
            Event.ANSWER_SUBMITTED,
            GuardContext(evaluator_correct=True, curator_next_state=State.TUTORIAL),
        )[0] is State.TUTORIAL

        retry = SessionStateMachine(State.LAB)
        assert retry.fire(
            Event.ANSWER_SUBMITTED,
            GuardContext(evaluator_correct=False, failed_attempts=0, max_attempts=2),
        )[0] is State.LAB

        exhausted = SessionStateMachine(State.LAB)
        target, effect = exhausted.fire(
            Event.ANSWER_SUBMITTED,
            GuardContext(evaluator_correct=False, failed_attempts=2, max_attempts=2),
        )
        assert (target, effect) == (State.TUTORIAL, "tutor_takes_over")

    @pytest.mark.parametrize(
        "state", [s for s in State if s not in (State.CLOSING,)]
    )
    def test_end_session_from_any_state_but_closing(self, state):
        """§7's 'any' row, with its 'Not CLOSING' guard."""
        assert SessionStateMachine(state).fire(Event.END_SESSION)[0] is State.CLOSING

    def test_end_session_from_closing_is_refused(self):
        with pytest.raises(IllegalTransition):
            SessionStateMachine(State.CLOSING).fire(Event.END_SESSION)

    def test_closing_returns_to_idle(self):
        assert SessionStateMachine(State.CLOSING).fire(Event.CLOSE_COMPLETE)[0] is State.IDLE


class TestPersistedMode:
    def test_runtime_only_states_do_not_change_the_persisted_mode(self):
        """§7: INTERRUPTED and PAUSED_FOR_QUESTION are '(unchanged from prior)'."""
        assert PERSISTED_MODE[State.INTERRUPTED] is None
        assert PERSISTED_MODE[State.PAUSED_FOR_QUESTION] is None

    def test_every_state_has_a_mode_entry(self):
        assert set(PERSISTED_MODE) == set(State)

    def test_persisted_modes_are_valid_session_mode_enum_values(self):
        """A mode the database enum rejects would fail at write, not here."""
        from studium.models.base import session_mode

        valid = set(session_mode.enums)
        for state, mode in PERSISTED_MODE.items():
            if mode is not None:
                assert mode in valid, f"{state} -> {mode!r} not in session_mode"

    def test_every_mode_entry_state_is_reachable(self):
        for mode, state in MODE_ENTRY_STATE.items():
            assert isinstance(state, State), mode


class TestLogAndTimeouts:
    def test_transitions_are_logged_in_memory(self):
        """§7: an in-memory log; session_turns carries the persistent record."""
        machine = SessionStateMachine(State.IDLE)
        machine.fire(Event.START_SESSION, GuardContext())
        machine.fire(Event.CONTEXT_READY, GuardContext(session_mode="lecture"))
        assert [r.event for r in machine.log] == [Event.START_SESSION, Event.CONTEXT_READY]

    def test_the_log_is_bounded(self):
        """An 8-hour session must not grow the log without limit."""
        machine = SessionStateMachine(State.LECTURING, log_size=4)
        for _ in range(20):
            machine.fire(Event.SEGMENT_COMPLETE, GuardContext(segments_remaining=1))
        assert len(machine.log) == 4

    def test_idle_timeout_measures_from_the_last_turn(self):
        """§7's timeout is about abandonment, not about session length.

        A learner still working at minute 200 of a 90-minute target is not
        timed out; one who stopped 106 minutes ago is.
        """
        now = dt.datetime(2026, 8, 16, 18, 0, tzinfo=dt.UTC)
        started = now - dt.timedelta(minutes=200)

        assert timed_out(
            started_at=started,
            last_turn_at=now - dt.timedelta(minutes=2),
            target_duration_minutes=90,
            now=now,
        ) is False

        assert timed_out(
            started_at=started,
            last_turn_at=now - dt.timedelta(minutes=106),
            target_duration_minutes=90,
            now=now,
        ) is True

    def test_only_streaming_states_are_interruptible(self):
        for state in (State.LECTURING, State.TUTORIAL):
            assert SessionStateMachine(state).is_interruptible is True
        for state in (State.LAB, State.CLOSING, State.IDLE, State.REVIEW):
            assert SessionStateMachine(state).is_interruptible is False


class TestReconstruction:
    def test_empty_history_falls_back_to_the_modes_entry_state(self):
        assert reconstruct_state([], mode="lecture") is State.LECTURING

    @pytest.mark.parametrize(
        ("actor", "mode", "expected"),
        [
            ("lecturer", "lecture", State.LECTURING),
            ("tutor", "tutorial", State.TUTORIAL),
            ("tutor", "office_hours", State.OFFICE_HOURS),
            ("reviewer", "review", State.REVIEW),
            ("evaluator", "lab", State.LAB),
            ("learner", "lecture", State.LECTURING),
        ],
    )
    def test_last_actor_decides_the_rebuilt_state(self, actor, mode, expected):
        """§7: reconstructible from the session_turns tail."""
        turns = [{"actor": "learner"}, {"actor": actor}]
        assert reconstruct_state(turns, mode=mode) is expected

    def test_reconstruction_never_returns_a_transient_state(self):
        """An interruption whose stream died with the process cannot resume.

        Returning PAUSED_FOR_QUESTION would leave the session waiting on a
        stream that no longer exists.
        """
        transient = {State.INTERRUPTED, State.PAUSED_FOR_QUESTION, State.CLOSING}
        for actor in ("lecturer", "tutor", "evaluator", "reviewer", "learner", "curator"):
            for mode in MODE_ENTRY_STATE:
                assert reconstruct_state([{"actor": actor}], mode=mode) not in transient
