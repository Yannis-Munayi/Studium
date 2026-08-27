"""Tutorial primitive dispatch (agent runtime §23 Tier 1, §15).

§23: "Primitive dispatch: ``dispatch_primitive('explain_differently', ...)``
invokes the expected agent methods with the expected arguments (mocked agents)."

What matters here is the routing, not the prose: which agent ran, with which
kind, in which order. Each of the eight rows in §15's table is checked against
the handler that implements it.
"""

from __future__ import annotations

import pytest

from studium.agents.base import AgentOutput
from studium.agents.schemas import (
    PRIMITIVE_NAMES,
    PracticeProblem,
    PracticeProblemForClient,
    TutorDiagnostic,
    VocabularyVerdict,
)
from studium.orchestration.primitives import (
    PRIMITIVE_HANDLERS,
    UnknownPrimitive,
    dispatch_primitive,
)
from studium.orchestration.state_machine import State
from tests.fixtures.runtime import (
    CONCEPT_ID,
    PREREQ_ID,
    FakeAgent,
    FakeRegistry,
    make_context,
)


async def _collect(name: str, agents: FakeRegistry, ctx=None, utterance: str = "") -> list:
    return [
        chunk
        async for chunk in dispatch_primitive(
            name, ctx or make_context(), agents, utterance=utterance
        )
    ]


def _kinds(agent: FakeAgent) -> list[str]:
    return [kind for kind, _ in agent.calls]


class TestDispatchTable:
    def test_every_primitive_the_classifier_can_emit_has_a_handler(self):
        """A valid classification with no handler dead-ends at dispatch."""
        assert set(PRIMITIVE_HANDLERS) == set(PRIMITIVE_NAMES)
        assert len(PRIMITIVE_NAMES) == 8

    async def test_unknown_primitive_raises(self):
        with pytest.raises(UnknownPrimitive):
            await _collect("teach_me_telepathy", FakeRegistry())


class TestExplainDifferently:
    async def test_tutor_diagnoses_then_lecturer_regenerates(self):
        """§15: 'Tutor first, then Lecturer' with a different stance."""
        agents = FakeRegistry()
        agents.tutor = FakeAgent(
            "tutor",
            outputs={
                "primitive:explain_differently": AgentOutput(
                    text="What part is not landing?",
                    structured=TutorDiagnostic(
                        chosen_stance="intuitive", what_is_not_landing="the notation"
                    ),
                )
            },
        )

        chunks = await _collect("explain_differently", agents, utterance="I'm stuck")

        assert _kinds(agents.tutor) == ["primitive:explain_differently"]
        assert _kinds(agents.lecturer) == ["re_explain"]
        assert agents.tutor.calls[0][1]["phase"] == "diagnose"

        # The stance the Tutor chose is what the Lecturer is told to use --
        # passed as a payload value, never as prompt text (§3).
        assert agents.lecturer.calls[0][1]["stance"] == "intuitive"
        assert agents.lecturer.calls[0][1]["what_is_not_landing"] == "the notation"
        assert chunks[-1].payload["stance"] == "intuitive"

    async def test_falls_back_to_a_different_stance_when_diagnosis_is_unstructured(self):
        """A missing structured output must not re-explain in the same stance."""
        agents = FakeRegistry()
        await _collect("explain_differently", agents)
        assert agents.lecturer.calls[0][1]["stance"] == "intuitive"


class TestVocabularyCheck:
    async def test_terminology_verdict_stops_after_the_paraphrase(self):
        """§15: if terminology, offer a paraphrase -- no Socratic sub-thread."""
        agents = FakeRegistry()
        agents.tutor = FakeAgent(
            "tutor",
            outputs={
                "primitive:vocabulary_check": AgentOutput(
                    text="'Redex' just means a reducible expression.",
                    structured=VocabularyVerdict(
                        kind="terminology", term="redex", paraphrase="reducible expression"
                    ),
                )
            },
        )
        await _collect("vocabulary_check", agents, utterance="what is a redex")
        assert _kinds(agents.tutor) == ["primitive:vocabulary_check"]

    async def test_substance_verdict_opens_a_socratic_sub_thread(self):
        """§15: if substance, do not paraphrase -- work the idea."""
        agents = FakeRegistry()
        agents.tutor = FakeAgent(
            "tutor",
            outputs={
                "primitive:vocabulary_check": AgentOutput(
                    text="Let's look at the idea itself.",
                    structured=VocabularyVerdict(kind="substance", term="redex"),
                )
            },
        )
        await _collect("vocabulary_check", agents, utterance="what is a redex")
        assert _kinds(agents.tutor) == ["primitive:vocabulary_check", "answer"]


class TestLetMeTryOne:
    async def test_curator_selects_and_the_session_moves_to_lab(self):
        """§15: 'Curator, then Lab'; §7: TUTORIAL --primitive--> LAB."""
        problem = PracticeProblem(
            prompt="Reduce (\\x. x x) (\\y. y) to normal form.",
            model_answer="\\y. y",
            expected_key_points=["substitute", "reduce"],
        )
        agents = FakeRegistry()
        agents.curator = FakeAgent(
            "curator", outputs={"select_practice": AgentOutput(text=problem.prompt, structured=problem)}
        )

        chunks = await _collect("let_me_try_one", agents)

        assert _kinds(agents.curator) == ["select_practice"]
        assert _kinds(agents.tutor) == []  # the Tutor does not invent problems
        end = chunks[-1]
        assert end.payload["next_state"] == State.LAB.value
        # The problem is carried forward so the LAB turn can grade against it
        # without paying for a second Curator call.
        assert end.payload["problem_private"]["expected_key_points"] == [
            "substitute",
            "reduce",
        ]

    async def test_the_answer_key_is_split_off_from_what_the_bench_renders(self):
        """§11.2 withholds the model answer until the learner has attempted.

        The bench having no component that draws it is not the same as it being
        absent: shipping the whole ``PracticeProblem`` on the `end` chunk would
        put the answer key in the browser next to the question. The split is
        made here; the Orchestrator strips ``problem_private`` before the chunk
        leaves the process.
        """
        problem = PracticeProblem(
            prompt="Reduce (\\x. x x) (\\y. y) to normal form.",
            model_answer="\\y. y",
            expected_key_points=["substitute", "reduce"],
            hint="Start with the outermost redex.",
        )
        agents = FakeRegistry()
        agents.curator = FakeAgent(
            "curator",
            outputs={"select_practice": AgentOutput(text=problem.prompt, structured=problem)},
        )

        end = (await _collect("let_me_try_one", agents))[-1]

        public = end.payload["problem"]
        assert public["prompt"] == problem.prompt
        assert "model_answer" not in public
        assert "expected_key_points" not in public

        # v1.0.1 §6.2: the ladder starts empty and grows as the learner clicks.
        # The first hint is not secret, it is not yet due -- so it is absent
        # here and held privately for release.
        assert public["hint_ladder"] == []

        # §6.3's real guarantee: the wire shape *is* the client model's shape,
        # so a field added to PracticeProblem is server-only by default rather
        # than public-unless-remembered-into-a-deny-list.
        assert set(public) == set(PracticeProblemForClient.model_fields)

        private = end.payload["problem_private"]
        assert private["model_answer"] == "\\y. y"

    async def test_a_curator_that_returned_nothing_structured_carries_no_problem(self):
        chunks = await _collect("let_me_try_one", FakeRegistry())
        assert chunks[-1].payload["problem"] is None
        assert chunks[-1].payload["problem_private"] is None


class TestShowWorkedExample:
    async def test_goes_straight_to_the_lecturer(self):
        """§15: this primitive is exposition, not dialogue."""
        agents = FakeRegistry()
        await _collect("show_worked_example", agents)
        assert _kinds(agents.lecturer) == ["worked_example"]
        assert _kinds(agents.tutor) == []


class TestImLost:
    async def test_refocuses_on_the_strongest_neighbouring_concept(self):
        """§15: reset focus to the last high-mastery concept in the neighbourhood."""
        agents = FakeRegistry()
        ctx = make_context(mastery_snapshot={str(PREREQ_ID): 0.93, str(CONCEPT_ID): 0.2})

        chunks = await _collect("im_lost", agents, ctx=ctx)

        assert _kinds(agents.tutor) == ["primitive:im_lost"]
        assert chunks[-1].payload["refocus_concept_id"] == str(PREREQ_ID)

    async def test_no_refocus_when_nothing_nearby_is_mastered(self):
        """A learner lost with no anchor gets the Tutor's question, not a bad bridge."""
        agents = FakeRegistry()
        ctx = make_context(mastery_snapshot={str(PREREQ_ID): 0.3, str(CONCEPT_ID): 0.2})
        chunks = await _collect("im_lost", agents, ctx=ctx)
        assert "refocus_concept_id" not in chunks[-1].payload


class TestTutorOnlyPrimitives:
    @pytest.mark.parametrize(
        "name", ["prove_it_to_me", "where_does_this_fit", "why_does_this_matter"]
    )
    async def test_route_to_the_tutor_alone(self, name):
        """§15's table: these three are Tutor-only."""
        agents = FakeRegistry()
        await _collect(name, agents)
        assert _kinds(agents.tutor) == [f"primitive:{name}"]
        assert _kinds(agents.lecturer) == []
        assert _kinds(agents.curator) == []


class TestStreamShape:
    @pytest.mark.parametrize("name", list(PRIMITIVE_NAMES))
    async def test_every_primitive_yields_exactly_one_terminal_chunk(self, name):
        """The client cannot tell a primitive from an ordinary turn (§15)."""
        chunks = await _collect(name, FakeRegistry())
        ends = [c for c in chunks if c.kind == "end"]
        assert len(ends) == 1
        assert chunks[-1] is ends[0]

    @pytest.mark.parametrize("name", list(PRIMITIVE_NAMES))
    async def test_every_primitive_produces_learner_visible_text(self, name):
        chunks = await _collect(name, FakeRegistry())
        assert any(c.kind == "text" and c.payload.get("text") for c in chunks)

    @pytest.mark.parametrize("name", list(PRIMITIVE_NAMES))
    async def test_the_terminal_chunk_names_the_primitive(self, name):
        chunks = await _collect(name, FakeRegistry())
        assert chunks[-1].payload["primitive"] == name
