"""The artifact id and the bench route, against a real model (§23 Tier 3).

Two claims that no mock can settle, because in both cases the mock is the thing
under suspicion.

**The artifact id.** SD5's fix is an ordering change: the Orchestrator holds the
`end` chunk until the turn's effects have committed, so the chunk can name the
`content_artifacts` row the segment became. Offline, the effect batch is faked,
so "the id arrives" is a statement about the fake. Here the Lecturer really
streams, the effect really writes, and the id on the chunk is really the primary
key of a row that `GET /api/artifacts/{id}/citations` can resolve.

**The bench route.** F15's fix spans the state machine, the primitive handler
and the Orchestrator. Offline each half is checked against a stub of the other.
Here a real Curator selects a real problem, and what is asserted is that the
model's answer key stays in the process while the question goes out.

Billable. Two Lecturer calls and one Curator call per run.

    STUDIUM_RUN_PAID_TESTS=1 python -m pytest tests/online/test_provenance_paid.py \\
        -m anthropic
"""

from __future__ import annotations

import pytest
from sqlalchemy import text as sql

from studium.agents.orchestrator import LearnerInput, Orchestrator
from studium.agents.schemas import PracticeProblem, PracticeProblemForClient
from studium.orchestration.handoff import AgentRegistry
from studium.orchestration.state_machine import State
from studium.retrieval import resolve_artifact_citations
from tests.fixtures.runtime import drive_to

pytestmark = [pytest.mark.postgres, pytest.mark.anthropic]


async def _run(orchestrator: Orchestrator, session_id, learner_input) -> list:
    chunks = [c async for c in orchestrator.handle_turn(session_id, learner_input)]
    await orchestrator._drain_background()
    return chunks


class TestArtifactProvenance:
    async def test_a_real_segment_names_the_artifact_it_became(
        self, seeded, paid_tests_enabled
    ):
        """SD5, closed and checked against the row it names."""
        db = seeded["db"]

        orchestrator = Orchestrator(agents=AgentRegistry.build())
        drive_to(orchestrator.machine, State.LECTURING)

        chunks = await _run(
            orchestrator, seeded["session_id"], LearnerInput(text="next", intent="next")
        )

        ends = [c for c in chunks if c.kind == "end"]
        assert ends, "the turn produced no terminal chunk"
        artifact_id = ends[-1].payload.get("artifact_id")
        assert artifact_id, (
            "the `end` chunk carries no artifact_id: either the Lecturer wrote no "
            "artifact, or the Orchestrator forwarded `end` before applying effects"
        )

        # The id is a real primary key, not an echo of something on the chunk.
        row = db.execute(
            sql(
                """
                SELECT kind, status, generated_by
                  FROM content_artifacts
                 WHERE id = CAST(:aid AS uuid)
                """
            ),
            {"aid": artifact_id},
        ).first()
        assert row is not None, f"no content_artifacts row for {artifact_id}"
        assert row.generated_by == "lecturer"
        # §6.4's lifecycle: a generated segment enters as a draft.
        assert row.status == "draft"

    async def test_the_turn_row_keeps_the_link_the_chunk_carried(
        self, seeded, paid_tests_enabled
    ):
        """A chunk is delivered once; a reloaded page needs the row (§6.6)."""
        db = seeded["db"]

        orchestrator = Orchestrator(agents=AgentRegistry.build())
        drive_to(orchestrator.machine, State.LECTURING)
        chunks = await _run(
            orchestrator, seeded["session_id"], LearnerInput(text="next", intent="next")
        )

        artifact_id = [c for c in chunks if c.kind == "end"][-1].payload.get("artifact_id")
        assert artifact_id

        linked = db.execute(
            sql(
                """
                SELECT count(*)
                  FROM session_turns
                 WHERE session_id = :sid
                   AND artifact_id = CAST(:aid AS uuid)
                """
            ),
            {"sid": seeded["session_id"], "aid": artifact_id},
        ).scalar_one()
        assert linked == 1, (
            "session_turns.artifact_id was not written: the chunk is then the "
            "only record of which artifact this turn produced"
        )

    async def test_the_id_resolves_through_the_citation_endpoint(
        self, seeded, paid_tests_enabled
    ):
        """The whole point of the id: §10's hover cards have something to ask.

        Asserts the *query path*, not a citation count. Whether a given segment
        cites anything depends on what the model wrote and on how well the
        concept is curated -- an ungrounded artifact returns an empty list and
        that is a real answer (retrieval §12). What must hold is that the id
        resolves at all rather than 404-ing.
        """
        db = seeded["db"]

        orchestrator = Orchestrator(agents=AgentRegistry.build())
        drive_to(orchestrator.machine, State.LECTURING)
        chunks = await _run(
            orchestrator, seeded["session_id"], LearnerInput(text="next", intent="next")
        )
        artifact_id = [c for c in chunks if c.kind == "end"][-1].payload.get("artifact_id")
        assert artifact_id

        import uuid as _uuid

        exists = db.execute(
            sql("SELECT 1 FROM content_artifacts WHERE id = CAST(:aid AS uuid)"),
            {"aid": artifact_id},
        ).scalar()
        assert exists, "the endpoint would 404 on the id the client was handed"

        citations = resolve_artifact_citations(db, _uuid.UUID(artifact_id))
        for citation in citations:
            assert citation.marker.startswith("P")
            assert citation.source_title


class TestPracticeProblemRouting:
    async def test_a_real_problem_reaches_lab_without_its_answer_key(
        self, seeded, paid_tests_enabled
    ):
        """F15 end to end: the Curator selects, the machine moves, the key stays.

        Run from LECTURING deliberately. That is where a lecture session sits
        and where the palette lives, and it is the state §7's table had no
        PRIMITIVE_INVOKED row for -- so this is also the R13 regression check
        against a real Curator rather than a stub.
        """
        orchestrator = Orchestrator(agents=AgentRegistry.build())
        drive_to(orchestrator.machine, State.LECTURING)

        chunks = await _run(
            orchestrator,
            seeded["session_id"],
            LearnerInput(text="", primitive="let_me_try_one"),
        )

        end = [c for c in chunks if c.kind == "end"][-1].payload
        assert end["next_state"] == State.LAB.value
        assert orchestrator.machine.state is State.LAB, (
            "the chunk announced LAB and the runtime stayed put: the learner's "
            "next answer would route to the Tutor instead of the Evaluator"
        )

        problem = end.get("problem")
        assert problem and problem.get("prompt"), "the Curator returned no problem"
        for withheld in PracticeProblem.SERVER_ONLY:
            assert withheld not in problem, (
                f"{withheld!r} reached the client: v1.0.1 §6.1 makes it "
                "server-only, and §11.2 withholds it until the learner has "
                "attempted"
            )
        # v1.0.1 §6.3: the wire shape is the client model's shape, so a field
        # added to the full problem is server-only until someone adds it here
        # on purpose. Asserting the key set rather than a deny-list is what
        # makes that default hold.
        assert set(problem) <= set(PracticeProblemForClient.model_fields)
        assert "problem_private" not in end

        # And the Orchestrator kept what the Evaluator will grade against.
        assert orchestrator.pending_problem is not None
        assert orchestrator.pending_problem.get("model_answer")
        assert orchestrator.pending_problem.get("prompt") == problem["prompt"]
