"""What the Orchestrator does around a turn's effects (§8, §20).

Two properties, both about ordering, both invisible in the agents themselves.

**The `end` chunk waits for the effect batch.** An agent returns effects rather
than applying them (§6), so the artifact a segment becomes has no id until the
Orchestrator commits the batch -- which used to happen *after* the terminal
chunk had already gone down the wire. Every citation marker in the product
therefore rendered against an artifact the client could not name (SPEC_DEBT
SD5). These assert that the id arrives on the chunk, and that a failed batch
degrades to "no id" rather than to a wrong one.

**The answer key does not leave the process.** `let_me_try_one` selects a
problem the Evaluator will grade against; the Orchestrator keeps the whole
thing and hands the client only what the bench draws.

Offline: the effect batch is faked at `apply_effects`, which is the seam
between the runtime and the data layer.
"""

from __future__ import annotations

import uuid

import pytest

from studium.agents.base import AgentOutput, StreamChunk, ToolEffect
from studium.agents.orchestrator import LearnerInput, Orchestrator
from studium.agents.schemas import PracticeProblem
from studium.orchestration import effects as effects_module
from studium.orchestration.state_machine import Event, GuardContext, State
from tests.fixtures.runtime import FakeAgent, FakeRegistry, drive_to, make_context


def _artifact_effect(kind: str, body: str) -> ToolEffect:
    """A `record_content_artifact` effect, minimal but well-formed."""
    return ToolEffect(
        kind="record_content_artifact",
        payload={"concept_id": str(uuid.uuid4()), "kind": kind, "body": body},
    )

ARTIFACT_ID = uuid.UUID("00000000-0000-7000-8000-0000000ca001")
TURN_ID = uuid.UUID("00000000-0000-7000-8000-0000000cb001")


@pytest.fixture
def applied(monkeypatch):
    """Record what was applied, and answer with a canned artifact id."""
    calls: list[dict[str, object]] = []

    async def fake_apply(effects, *, session_turn_id=None):
        calls.append({"effects": list(effects), "turn_id": session_turn_id})
        wrote_artifact = any(e.kind == "record_content_artifact" for e in effects)
        return effects_module.AppliedEffects(
            kinds=[e.kind for e in effects],
            artifact_ids=[ARTIFACT_ID] if wrote_artifact else [],
        )

    monkeypatch.setattr(effects_module, "apply_effects", fake_apply)
    return calls


def _lecturer_writing_an_artifact() -> FakeRegistry:
    agents = FakeRegistry()
    agents.lecturer = FakeAgent(
        "lecturer",
        outputs={
            "deliver_segment": AgentOutput(
                text="Beta reduction rewrites a redex [P1].",
                tool_effects=[
                    _artifact_effect(
                        "lecture_segment", "Beta reduction rewrites a redex [P1]."
                    )
                ],
            )
        },
    )
    return agents


async def _drain(iterator) -> list[StreamChunk]:
    return [chunk async for chunk in iterator]


class TestArtifactIdReachesTheClient:
    async def test_the_end_chunk_names_the_artifact_the_segment_became(self, applied):
        """SD5: the one field that makes every hover card in §10 resolvable."""
        orchestrator = Orchestrator(agents=_lecturer_writing_an_artifact())
        drive_to(orchestrator.machine, State.LECTURING)

        chunks = await _drain(
            orchestrator._handle_conversational(make_context(), "next", LearnerInput())
        )

        end = chunks[-1]
        assert end.kind == "end"
        assert end.payload["artifact_id"] == str(ARTIFACT_ID)

    async def test_the_end_chunk_is_emitted_after_the_effects_are_applied(self, applied):
        """The ordering is the fix, not a side effect of it.

        The id cannot be on the chunk unless the chunk waits for the batch, so
        this asserts the sequence rather than only the field -- a future change
        that forwarded `end` early would still pass the test above if it also
        happened to attach a stale id.
        """
        seen: list[str] = []

        async def fake_apply(effects, *, session_turn_id=None):
            seen.append("applied")
            return effects_module.AppliedEffects(artifact_ids=[ARTIFACT_ID])

        orchestrator = Orchestrator(agents=_lecturer_writing_an_artifact())
        drive_to(orchestrator.machine, State.LECTURING)

        import studium.agents.orchestrator as module

        original = module.effects_module.apply_effects
        module.effects_module.apply_effects = fake_apply  # type: ignore[assignment]
        try:
            async for chunk in orchestrator._handle_conversational(
                make_context(), "next", LearnerInput()
            ):
                if chunk.kind == "end":
                    seen.append("end")
        finally:
            module.effects_module.apply_effects = original  # type: ignore[assignment]

        assert seen == ["applied", "end"]

    async def test_a_tutor_turn_carries_no_artifact_id(self, applied):
        """Optional, not required: most turns produce nothing to cite."""
        orchestrator = Orchestrator(agents=FakeRegistry())
        drive_to(orchestrator.machine, State.TUTORIAL)

        chunks = await _drain(
            orchestrator._handle_conversational(
                make_context(), "question", LearnerInput("why?")
            )
        )

        assert chunks[-1].kind == "end"
        assert "artifact_id" not in chunks[-1].payload

    async def test_a_rolled_back_batch_leaves_the_id_off_rather_than_guessing(
        self, monkeypatch
    ):
        """§21's degradation, read through F3's lens.

        A failed batch means the artifact does not exist. Naming one anyway
        would send the client to a 404; naming none renders the marker with the
        card that says the source is not linked, which is the truth.
        """
        async def failing_apply(effects, *, session_turn_id=None):
            raise effects_module.EffectApplicationError(
                "record_content_artifact", RuntimeError("constraint")
            )

        monkeypatch.setattr(effects_module, "apply_effects", failing_apply)

        orchestrator = Orchestrator(agents=_lecturer_writing_an_artifact())
        drive_to(orchestrator.machine, State.LECTURING)

        chunks = await _drain(
            orchestrator._handle_conversational(make_context(), "next", LearnerInput())
        )

        assert chunks[-1].kind == "end"
        assert chunks[-1].payload.get("artifact_id") is None

    async def test_the_effect_batch_is_anchored_to_the_turn_the_agent_wrote(
        self, applied
    ):
        """`session_turns.artifact_id` is written from this pairing."""
        agents = _lecturer_writing_an_artifact()

        async def streaming_with_a_turn_id(input):
            yield StreamChunk.text_chunk("Beta reduction rewrites a redex [P1].")
            yield StreamChunk.effect(_artifact_effect("lecture_segment", "body"))
            yield StreamChunk.ended(turn_id=str(TURN_ID), segment_index=3)

        agents.lecturer.handle_streaming = streaming_with_a_turn_id  # type: ignore[method-assign]

        orchestrator = Orchestrator(agents=agents)
        drive_to(orchestrator.machine, State.LECTURING)
        await _drain(
            orchestrator._handle_conversational(make_context(), "next", LearnerInput())
        )

        assert applied[0]["turn_id"] == TURN_ID


class TestPrimitivesCarryTheirArtifact:
    async def test_show_worked_example_names_its_artifact(self, applied):
        """The Lecturer runs inside this primitive, so it writes one too."""
        orchestrator = Orchestrator(agents=_lecturer_showing_a_worked_example())
        drive_to(orchestrator.machine, State.TUTORIAL)

        chunks = await _drain(
            orchestrator._handle_primitive(
                make_context(), "primitive:show_worked_example", LearnerInput()
            )
        )

        assert chunks[-1].payload["artifact_id"] == str(ARTIFACT_ID)


def _lecturer_showing_a_worked_example() -> FakeRegistry:
    agents = FakeRegistry()
    agents.lecturer = FakeAgent(
        "lecturer",
        outputs={
            "worked_example": AgentOutput(
                text="Take (\\x. x x) (\\y. y).",
                tool_effects=[
                    _artifact_effect("worked_example", "Take (\\x. x x) (\\y. y).")
                ],
            )
        },
    )
    return agents


class TestPracticeProblemRedaction:
    """The bench gets the question; the Evaluator keeps the answer key."""

    def _agents_with_a_problem(self) -> tuple[FakeRegistry, PracticeProblem]:
        problem = PracticeProblem(
            prompt="Reduce (\\x. x x) (\\y. y) to normal form.",
            model_answer="\\y. y",
            expected_key_points=["substitute", "reduce"],
            hint="Start with the outermost redex.",
            difficulty=2,
        )
        agents = FakeRegistry()
        agents.curator = FakeAgent(
            "curator",
            outputs={
                "select_practice": AgentOutput(text=problem.prompt, structured=problem)
            },
        )
        return agents, problem

    async def test_the_client_sees_the_question_and_not_the_answer(self, applied):
        agents, _ = self._agents_with_a_problem()
        orchestrator = Orchestrator(agents=agents)
        drive_to(orchestrator.machine, State.LECTURING)

        chunks = await _drain(
            orchestrator._handle_primitive(
                make_context(), "primitive:let_me_try_one", LearnerInput()
            )
        )

        end = chunks[-1].payload
        assert end["problem"]["prompt"].startswith("Reduce")
        # §6.2: no hint until the learner asks for one.
        assert end["problem"]["hint_ladder"] == []
        assert "model_answer" not in end["problem"]
        assert "expected_key_points" not in end["problem"]
        # The private half never reaches the wire at all.
        assert "problem_private" not in end

    async def test_the_orchestrator_keeps_the_whole_problem_to_grade_against(
        self, applied
    ):
        agents, problem = self._agents_with_a_problem()
        orchestrator = Orchestrator(agents=agents)
        drive_to(orchestrator.machine, State.LECTURING)

        await _drain(
            orchestrator._handle_primitive(
                make_context(), "primitive:let_me_try_one", LearnerInput()
            )
        )

        assert orchestrator.pending_problem is not None
        assert orchestrator.pending_problem["model_answer"] == problem.model_answer
        assert orchestrator.pending_problem["expected_key_points"] == [
            "substitute",
            "reduce",
        ]
        assert orchestrator.pending_problem["prompt"] == problem.prompt
        assert orchestrator.failed_attempts == 0


class TestLeavingOpening:
    """§16's opening step, which nothing used to fire (R14).

    ``_leave_opening`` is tested directly rather than through ``handle_turn``:
    the full turn needs a database for its context assembly and its budget gate,
    and the property here is about the state machine, not about either.
    """

    def _orchestrator(self) -> Orchestrator:
        orchestrator = Orchestrator(agents=FakeRegistry())
        drive_to(orchestrator.machine, State.OPENING)
        return orchestrator

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("lecture", State.LECTURING),
            ("tutorial", State.TUTORIAL),
            ("lab", State.LAB),
            ("office_hours", State.OFFICE_HOURS),
            ("orientation", State.TUTORIAL),
        ],
    )
    def test_the_first_turn_enters_the_modes_state(self, mode, expected):
        orchestrator = self._orchestrator()
        orchestrator._leave_opening(make_context(session={**make_context().session, "mode": mode}))
        assert orchestrator.machine.state is expected

    def test_a_session_left_in_opening_can_neither_be_interrupted_nor_moved(self):
        """Why R14 mattered, stated as the two things OPENING forbids.

        This is the state a real session used to spend its whole life in. Both
        assertions are about the machine rather than about the fix, so they keep
        describing the hazard even if the fix moves.
        """
        stuck = self._orchestrator()
        assert stuck.machine.is_interruptible is False
        assert stuck.machine.can(
            Event.PRIMITIVE_INVOKED, GuardContext(primitive="let_me_try_one")
        ) is False

    def test_it_fires_once_and_leaves_later_turns_alone(self):
        orchestrator = self._orchestrator()
        ctx = make_context()

        orchestrator._leave_opening(ctx)
        assert orchestrator.machine.state is State.LECTURING

        # A second turn must not re-enter the mode's state from wherever the
        # session has since moved to.
        drive_to(orchestrator.machine, State.LAB)
        orchestrator._leave_opening(ctx)
        assert orchestrator.machine.state is State.LAB

    def test_a_prior_summary_takes_the_retrieval_check_branch(self):
        """§7 splits the row; §16 runs the check. Both land on the mode's state.

        The branch is recorded in the transition log rather than in the target,
        which is what R12 settled -- so this asserts the effect name, because
        that is the only place the two paths differ.
        """
        orchestrator = self._orchestrator()
        ctx = make_context(prior_session_summary={"summary": "Last time: redexes."})

        orchestrator._leave_opening(ctx)

        assert orchestrator.machine.state is State.LECTURING
        assert orchestrator.machine.log[-1].effect == "run_retrieval_check"

    def test_no_prior_summary_skips_the_check(self):
        orchestrator = self._orchestrator()
        orchestrator._leave_opening(make_context(prior_session_summary=None))
        assert orchestrator.machine.log[-1].effect == "enter_mode_state"


class TestLetMeTryOneReachesTheLab:
    @pytest.mark.parametrize(
        "source", [State.TUTORIAL, State.LECTURING, State.PAUSED_FOR_QUESTION]
    )
    async def test_the_machine_moves_with_the_chunk(self, applied, source):
        """R13: the `end` chunk and the runtime must agree about the state.

        They did not from LECTURING, which is where a lecture session sits and
        where the palette lives. The chunk said LAB, the machine stayed put, and
        the learner's answer routed to the Tutor instead of being graded.
        """
        agents = FakeRegistry()
        problem = PracticeProblem(prompt="Reduce it.", model_answer="\\y. y")
        agents.curator = FakeAgent(
            "curator",
            outputs={"select_practice": AgentOutput(text=problem.prompt, structured=problem)},
        )

        orchestrator = Orchestrator(agents=agents)
        drive_to(orchestrator.machine, source)

        chunks = await _drain(
            orchestrator._handle_primitive(
                make_context(), "primitive:let_me_try_one", LearnerInput()
            )
        )

        assert chunks[-1].payload["next_state"] == State.LAB.value
        assert orchestrator.machine.state is State.LAB

    @pytest.mark.parametrize(
        "source", [State.TUTORIAL, State.LECTURING, State.PAUSED_FOR_QUESTION]
    )
    async def test_the_other_primitives_do_not_move_the_session(self, applied, source):
        orchestrator = Orchestrator(agents=FakeRegistry())
        drive_to(orchestrator.machine, source)

        await _drain(
            orchestrator._handle_primitive(
                make_context(), "primitive:why_does_this_matter", LearnerInput()
            )
        )

        assert orchestrator.machine.state is source

    async def test_a_primitive_from_a_state_with_no_row_is_not_an_error(self, applied):
        """OFFICE_HOURS has no PRIMITIVE_INVOKED row and must not raise.

        The guard is `can`, not a try/except around `fire`: a primitive invoked
        where §7 declares no transition runs and leaves the state alone, which
        is what the seven non-LAB primitives do everywhere else anyway.
        """
        orchestrator = Orchestrator(agents=FakeRegistry())
        drive_to(orchestrator.machine, State.OFFICE_HOURS)

        chunks = await _drain(
            orchestrator._handle_primitive(
                make_context(), "primitive:im_lost", LearnerInput()
            )
        )

        assert chunks[-1].kind == "end"
        assert orchestrator.machine.state is State.OFFICE_HOURS
