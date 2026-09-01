"""Tier 2: online integration (agent runtime §23).

The five checks §23 asks for, each gated on what it actually needs:

* the database-only ones (``@pytest.mark.postgres``) exercise effect
  application, trace writing, and context assembly against real Postgres 16
  with a fake model;
* the billable ones (``@pytest.mark.anthropic``) additionally require an API
  key and an explicit opt-in, because they spend money.

Splitting them that way matters: four of the five §23 checks are really about
*persistence*, and gating those behind an API key would leave them unrun in
every environment that has a database but no billing.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text as sql

from studium.agents.base import ToolEffect
from studium.agents.orchestrator import LearnerInput, Orchestrator
from studium.orchestration import effects as effects_module
from studium.orchestration.handoff import AgentRegistry
from studium.orchestration.state_machine import State
from studium.retrieval import CuratedPointerRetriever
from studium.session import memory
from studium.session.escalation import (
    escalation_status,
    should_offer_escalation,
)
from tests.fixtures.runtime import drive_to

pytestmark = pytest.mark.postgres


class TestContextAssembly:
    """§6: agents read one assembled context and never query the database."""

    async def test_assembles_a_complete_context_from_a_seeded_session(self, seeded):
        ctx = await memory.assemble_context(seeded["session_id"])

        assert ctx.session_id == seeded["session_id"]
        # The fixture suffixes the slug per build so parallel runs cannot collide.
        assert ctx.subject["slug"].startswith("lambda-calculus")
        assert ctx.focus_concept is not None
        assert ctx.focus_concept["slug"] == "beta-reduction"
        assert len(ctx.subject_concepts) == 6
        assert ctx.grounding_version

    async def test_mastery_is_decayed_at_read_time(self, seeded):
        """Gating on a stale p_known_decayed would let faded evidence through."""
        db = seeded["db"]
        fixture = seeded["fixture"]

        # The fixture already seeds concept_mastery rows, so this upserts rather
        # than inserting: p_known 1.0 with evidence 60 days old.
        db.execute(
            sql(
                """
                INSERT INTO concept_mastery
                    (learner_subject_id, concept_id, p_known, p_known_decayed,
                     evidence_count, last_evidence_at)
                VALUES (:lsid, :cid, 1.0, 1.0, 1, NOW() - INTERVAL '60 days')
                ON CONFLICT (learner_subject_id, concept_id) DO UPDATE
                   SET p_known         = 1.0,
                       p_known_decayed = 1.0,
                       last_evidence_at = NOW() - INTERVAL '60 days'
                """
            ),
            {"lsid": fixture.enrollment.id, "cid": fixture.concept_id("syntax")},
        )
        db.flush()

        ctx = await memory.assemble_context(seeded["session_id"])
        decayed = ctx.mastery_of(fixture.concept_id("syntax"))

        # 60 days is two 30-day half-lives: 1.0 -> ~0.25, not the stored 1.0.
        assert 0.2 < decayed < 0.3


class TestEffectApplication:
    """§8: all effects for a turn in one transaction, or none of them."""

    async def test_mastery_evidence_creates_the_row_and_the_event(self, seeded):
        db, fixture = seeded["db"], seeded["fixture"]

        await effects_module.apply_effects(
            [
                ToolEffect(
                    kind="record_mastery_evidence",
                    payload={
                        "learner_subject_id": str(fixture.enrollment.id),
                        "concept_id": str(fixture.concept_id("beta-reduction")),
                        "session_id": str(seeded["session_id"]),
                        "kind": "practice_correct",
                        "correct": True,
                        "evidence": {"verdict": "correct"},
                    },
                )
            ]
        )

        events = db.execute(
            sql(
                """
                SELECT me.kind, me.p_known_before, me.p_known_after
                  FROM mastery_events me
                  JOIN concept_mastery cm ON cm.id = me.concept_mastery_id
                 WHERE cm.learner_subject_id = :lsid
                """
            ),
            {"lsid": fixture.enrollment.id},
        ).all()

        assert len(events) == 1
        assert events[0].kind == "practice_correct"
        assert events[0].p_known_after > events[0].p_known_before

    async def test_a_failing_effect_rolls_back_the_whole_batch(self, seeded):
        """§8: 'If any effect fails, all effects for the turn are rolled back.'"""
        fixture = seeded["fixture"]

        good = ToolEffect(
            kind="record_mastery_evidence",
            payload={
                "learner_subject_id": str(fixture.enrollment.id),
                "concept_id": str(fixture.concept_id("beta-reduction")),
                "session_id": str(seeded["session_id"]),
                "kind": "practice_correct",
                "correct": True,
            },
        )
        # A journal revision against an id that does not exist is skipped, so
        # force a real failure: a concept id that violates the foreign key.
        bad = ToolEffect(
            kind="record_mastery_evidence",
            payload={
                "learner_subject_id": str(fixture.enrollment.id),
                "concept_id": str(uuid.uuid4()),
                "session_id": str(seeded["session_id"]),
                "kind": "practice_correct",
                "correct": True,
            },
        )

        with pytest.raises(effects_module.EffectApplicationError):
            await effects_module.apply_effects([good, bad])

    async def test_a_batch_carrying_a_portfolio_item_commits_whole(self, seeded):
        """E9 the other way round: the rollback rule above, but proving the
        batch no longer trips it.

        `_record_portfolio_item` never built the `NOT NULL` signature manifest,
        so it raised at insert and the rule under test above did the rest --
        the turn lost its mastery evidence and its journal update as well. The
        handler-level tests in ``test_evaluation_db.py`` call the handler
        directly and so cannot see that consequence; it needs a mixed batch.
        See DIVERGENCES-EVALUATION (E9).
        """
        db, fixture = seeded["db"], seeded["fixture"]
        session_id = seeded["session_id"]

        applied = await effects_module.apply_effects(
            [
                ToolEffect(
                    kind="record_mastery_evidence",
                    payload={
                        "learner_subject_id": str(fixture.enrollment.id),
                        "concept_id": str(fixture.concept_id("beta-reduction")),
                        "session_id": str(session_id),
                        "kind": "practice_correct",
                        "correct": True,
                        "evidence": {"verdict": "correct"},
                    },
                ),
                ToolEffect(
                    kind="update_journal",
                    payload={
                        "action": "create_entry",
                        "user_id": str(fixture.user.id),
                        "learner_subject_id": str(fixture.enrollment.id),
                        "session_id": str(session_id),
                        "summary": "Confuses beta-reduction with substitution.",
                        "hypothesis": "Applies the rule without renaming.",
                    },
                ),
                ToolEffect(
                    kind="record_portfolio_item",
                    payload={
                        "user_id": str(fixture.user.id),
                        "learner_subject_id": str(fixture.enrollment.id),
                        "session_id": str(session_id),
                        "body": "A proof the learner wrote.",
                        "kind": "proof",
                        "title": "Beta-reduction is confluent",
                    },
                ),
            ]
        )

        assert set(applied.kinds) == {
            "record_mastery_evidence",
            "update_journal",
            "record_portfolio_item",
        }

        mastery = db.execute(
            sql(
                """
                SELECT count(*) FROM mastery_events me
                  JOIN concept_mastery cm ON cm.id = me.concept_mastery_id
                 WHERE cm.learner_subject_id = :lsid
                """
            ),
            {"lsid": fixture.enrollment.id},
        ).scalar_one()
        journal = db.execute(
            sql("SELECT count(*) FROM journal_entries WHERE learner_subject_id = :lsid"),
            {"lsid": fixture.enrollment.id},
        ).scalar_one()
        signature = db.execute(
            sql(
                "SELECT signature FROM portfolio_items WHERE learner_subject_id = :lsid"
            ),
            {"lsid": fixture.enrollment.id},
        ).scalar_one()

        # The two collateral losses are asserted by name: a bare "the item is
        # there" would still pass if the batch had been rolled back and only
        # the portfolio write retried.
        assert mastery == 1, "mastery evidence was rolled back with the batch"
        assert journal == 1, "journal update was rolled back with the batch"
        assert signature is not None

    async def test_a_persisted_artifact_reports_its_id_and_links_its_turn(
        self, seeded
    ):
        """SD5's other half: the id has to come *back*, and it has to persist.

        The `end` chunk carries it to the client, which is what makes §10's
        hover cards resolvable. But a chunk is delivered once -- a reloaded page
        has no way back to it -- so ``session_turns.artifact_id`` is where the
        same fact survives a refresh. The column has been in the schema since
        §6.6 and nothing had ever written it.
        """
        db, fixture = seeded["db"], seeded["fixture"]

        turn_id = db.execute(
            sql(
                """
                INSERT INTO session_turns (session_id, turn_index, actor, input, output)
                VALUES (:sid, 7, 'lecturer', '{}'::jsonb, '{}'::jsonb)
                RETURNING id
                """
            ),
            {"sid": seeded["session_id"]},
        ).scalar_one()
        db.flush()

        applied = await effects_module.apply_effects(
            [
                ToolEffect(
                    kind="record_content_artifact",
                    payload={
                        "concept_id": str(fixture.concept_id("beta-reduction")),
                        "kind": "lecture_segment",
                        "stance": "formal",
                        "body": "A redex is an application of an abstraction [P1].",
                        "generated_by": "lecturer",
                        "session_id": str(seeded["session_id"]),
                        "citations": [
                            {"source_chunk_id": str(fixture.chunks[0].id)}
                        ],
                    },
                )
            ],
            session_turn_id=turn_id,
        )

        assert applied.kinds == ["record_content_artifact"]
        artifact_id = applied.artifact_id
        assert artifact_id is not None

        stored = db.execute(
            sql("SELECT artifact_id FROM session_turns WHERE id = :turn_id"),
            {"turn_id": turn_id},
        ).scalar_one()
        assert stored == artifact_id

        # And the id resolves through the endpoint the client will call.
        cited = db.execute(
            sql("SELECT count(*) FROM content_citations WHERE artifact_id = :aid"),
            {"aid": artifact_id},
        ).scalar_one()
        assert cited == 1

    async def test_review_flags_attach_to_the_turn_when_no_artifact_is_named(
        self, seeded
    ):
        """content_review_queue CHECKs that a flag has a subject."""
        db = seeded["db"]

        turn_id = db.execute(
            sql(
                """
                INSERT INTO session_turns (session_id, turn_index, actor, input, output)
                VALUES (:sid, 0, 'lecturer', '{}'::jsonb, '{}'::jsonb)
                RETURNING id
                """
            ),
            {"sid": seeded["session_id"]},
        ).scalar_one()
        db.flush()

        await effects_module.apply_effects(
            [
                ToolEffect(
                    kind="flag_for_review",
                    payload={
                        "source": "system_confidence",
                        "severity": 2,
                        "reason": "thin grounding",
                    },
                )
            ],
            session_turn_id=turn_id,
        )

        row = db.execute(
            sql("SELECT session_turn_id, severity FROM content_review_queue")
        ).one()
        assert row.session_turn_id == turn_id
        assert row.severity == 2

    async def test_a_flag_with_no_target_is_dropped_rather_than_failing_the_batch(
        self, seeded
    ):
        """A diagnostic must never take a learner's turn down with it."""
        applied = await effects_module.apply_effects(
            [ToolEffect(kind="flag_for_review", payload={"reason": "orphan"})]
        )
        assert applied.kinds == ["flag_for_review"]
        assert seeded["db"].execute(
            sql("SELECT count(*) FROM content_review_queue")
        ).scalar_one() == 0


class TestRetrieval:
    async def test_curated_pointer_retriever_returns_grounding(self, seeded):
        """The zero-dependency retriever must actually ground a lecture.

        It now returns a ``RetrievalResult`` rather than a bare list: subsystem
        3 needed the thin-grounding verdict to reach the caller, and §13 makes
        responding to that verdict the caller's job. See
        DIVERGENCES-RETRIEVAL (S4)."""
        fixture = seeded["fixture"]

        result = await CuratedPointerRetriever().retrieve_passages(
            fixture.concept_id("beta-reduction"), k=6
        )
        assert result.passages
        for passage in result.passages:
            assert passage.text
            assert passage.chunk_id


@pytest.mark.anthropic
class TestBillableIntegration:
    """The three §23 checks that genuinely need a live model."""

    async def test_end_to_end_session_smoke(self, seeded, paid_tests_enabled):
        """§23: open a session, run turns, close it; assert the trail exists."""
        db = seeded["db"]

        orchestrator = Orchestrator(agents=AgentRegistry.build())
        drive_to(orchestrator.machine, State.TUTORIAL)

        chunks = [
            c
            async for c in orchestrator.handle_turn(
                seeded["session_id"],
                LearnerInput(text="What is a redex, exactly?"),
            )
        ]
        await orchestrator._drain_background()

        assert any(c.kind == "text" for c in chunks)

        turns = db.execute(
            sql("SELECT actor FROM session_turns WHERE session_id = :sid ORDER BY turn_index"),
            {"sid": seeded["session_id"]},
        ).scalars().all()
        assert "learner" in turns

        traces = db.execute(
            sql(
                """
                SELECT t.agent, t.model, t.cost_usd, t.tokens_out
                  FROM agent_traces t
                  JOIN session_turns st ON st.id = t.session_turn_id
                 WHERE st.session_id = :sid
                """
            ),
            {"sid": seeded["session_id"]},
        ).all()
        assert traces, "no agent_traces rows: a billable call went unaccounted for"
        for trace in traces:
            assert trace.cost_usd > 0
            assert trace.model in {"claude-opus-4-8", "claude-haiku-4-5"}

    async def test_grading_integrity(self, seeded, paid_tests_enabled):
        """§23: a confidently-worded wrong answer must score 0.

        This is the one behavioural claim the whole grading design rests on --
        §12's "Do not reward confident tone, length, or vocabulary without
        substance."
        """
        from studium.agents.base import AgentInput
        from studium.agents.evaluator import Evaluator
        from studium.llm.client import AnthropicClient

        ctx = await memory.assemble_context(seeded["session_id"])
        graded = await Evaluator(AnthropicClient()).handle(
            AgentInput(
                session_context=ctx,
                kind="grade_practice",
                payload={
                    "question": "State the beta-reduction rule.",
                    "expected_key_points": [
                        "(\\x. M) N reduces to M with N substituted for x",
                        "substitution must avoid variable capture",
                    ],
                    "answer": (
                        "Absolutely -- beta-reduction is the foundational "
                        "principle whereby the lambda calculus achieves its "
                        "remarkable computational universality, a profound and "
                        "elegant result of enormous significance."
                    ),
                    "attempt": 1,
                },
            )
        )
        assert graded.structured.verdict == "incorrect"

    async def test_cache_hit_rate_climbs_across_turns_on_one_concept(self, seeded, paid_tests_enabled):
        """§23 wants >0.6 cache-read share after 10 turns on a concept.

        Asserted here at a lower bar over fewer turns, because the check is
        whether caching engages at all -- the 60-80% figure in §17 is a
        projection §25 lists as an open question, to be measured in Langfuse
        against real sessions rather than pinned by a test.
        """
        from studium.agents.base import AgentInput
        from studium.agents.tutor import Tutor
        from studium.llm.client import AnthropicClient

        ctx = await memory.assemble_context(seeded["session_id"])
        tutor = Tutor(AnthropicClient(), CuratedPointerRetriever())

        for turn in range(3):
            async for _ in tutor.handle_streaming(
                AgentInput(
                    session_context=ctx,
                    kind="answer",
                    payload={"utterance": f"Question {turn}: why does that hold?"},
                )
            ):
                pass

        rows = seeded["db"].execute(
            sql(
                """
                SELECT t.cache_read_tokens, t.tokens_in, t.cache_write_1h_tokens,
                       t.cache_write_5m_tokens
                  FROM agent_traces t
                  JOIN session_turns st ON st.id = t.session_turn_id
                 WHERE st.session_id = :sid AND t.agent = 'tutor'
                 ORDER BY t.created_at
                """
            ),
            {"sid": seeded["session_id"]},
        ).all()

        assert len(rows) >= 3
        later = rows[1:]
        assert any(r.cache_read_tokens > 0 for r in later), (
            "no cache reads after the first turn: the prefix is not byte-stable "
            "in practice, or it is under the model's cache minimum"
        )


class TestSessionOpenEndToEnd:
    """§7.1's third requirement: the open path, driven from the HTTP boundary.

    R14 was a session that opened into OPENING and stayed there, because
    nothing fired CONTEXT_READY. Every test that mattered started from a state
    it had assigned, so the gap between "the transition exists" and "something
    performs it" was invisible in all four tiers at once.

    This test closes that gap the only way it can be closed: by starting where
    a learner starts -- an HTTP request and nothing else -- and asserting on
    where the session lands. It uses a fake model but a real database, real
    context assembly, and the real state machine, because the defect was never
    in the model.
    """

    @pytest.fixture
    def client(self, seeded):
        from fastapi.testclient import TestClient

        from studium.api.app import app, registry
        from tests.fixtures.runtime import FakeRegistry

        registry._agents = FakeRegistry()  # type: ignore[attr-defined]
        with TestClient(app) as c:
            yield c
        registry._by_session.clear()
        registry._agents = None  # type: ignore[attr-defined]

    async def test_opening_a_lecture_session_reaches_lecturing(self, client, seeded):
        """IDLE -> OPENING -> LECTURING, with no state assigned by the test."""
        fixture = seeded["fixture"]

        response = client.post(
            "/api/session",
            json={
                "user_id": str(fixture.user.id),
                "mode": "lecture",
                "focus_concept_id": str(fixture.concept_id("beta-reduction")),
                "learner_subject_id": str(fixture.enrollment.id),
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()

        # The assertion §7.1 asks for. OPENING here is the R14 signature: the
        # session opened and then nothing moved it, which is a hang the learner
        # experiences as a blank screen.
        assert body["state"] == "LECTURING", (
            f"session opened into {body['state']} and stopped. A session that "
            f"cannot leave OPENING renders nothing -- this is R14's signature."
        )
        assert body["mode"] == "lecture"

    async def test_the_lecturer_produces_a_segment_on_the_opened_session(
        self, client, seeded
    ):
        """§7.1: "the Lecturer produces at least one segment."

        Reaching LECTURING is necessary and not sufficient: a session in the
        right state that emits nothing is the same blank screen with a better
        state field.
        """
        fixture = seeded["fixture"]

        opened = client.post(
            "/api/session",
            json={
                "user_id": str(fixture.user.id),
                "mode": "lecture",
                "focus_concept_id": str(fixture.concept_id("beta-reduction")),
                "learner_subject_id": str(fixture.enrollment.id),
            },
        ).json()

        turn = client.post(
            f"/api/session/{opened['session_id']}/turn",
            json={"text": "Start the lecture."},
        )

        assert turn.status_code == 200
        assert turn.headers["content-type"].startswith("text/event-stream")
        assert "data: " in turn.text
        # A degraded chunk is a stream that terminated on an error path; it
        # satisfies "produced output" while producing no teaching.
        assert "degraded" not in turn.text, turn.text[:500]

    async def test_the_transition_log_records_the_real_path(self, client, seeded):
        """Not just the destination -- the route taken to it.

        A session that arrived at LECTURING by some other path would pass the
        first test. The log is what distinguishes the documented open sequence
        from a shortcut that happens to land in the same place.
        """
        fixture = seeded["fixture"]

        opened = client.post(
            "/api/session",
            json={
                "user_id": str(fixture.user.id),
                "mode": "lecture",
                "focus_concept_id": str(fixture.concept_id("beta-reduction")),
                "learner_subject_id": str(fixture.enrollment.id),
            },
        ).json()

        state = client.get(f"/api/session/{opened['session_id']}/state").json()
        path = [(t["from"], t["event"], t["to"]) for t in state["transitions"]]

        assert ("IDLE", "start_session", "OPENING") in path
        assert ("OPENING", "context_ready", "LECTURING") in path


class TestEscalationThreshold:
    """§5.3: one computation, two callers.

    The client renders the button from this and the server verifies the press
    against it. Two implementations would drift, and the drift would surface as
    a button that 400s when pressed -- so what is worth testing here is the
    computation itself, against real turns, at the boundaries.
    """

    @staticmethod
    def _turn(db, session_id, index, actor, *, intent=None, minutes_ago=0):
        db.execute(
            sql(
                """
                INSERT INTO session_turns
                    (session_id, turn_index, actor, input, output, created_at)
                VALUES (:sid, :idx, :actor, CAST(:input AS jsonb), '{}'::jsonb,
                        NOW() - make_interval(mins => :mins))
                """
            ),
            {
                "sid": session_id,
                "idx": index,
                "actor": actor,
                "input": json.dumps({"intent": intent} if intent else {}),
                "mins": minutes_ago,
            },
        )
        db.flush()

    async def test_a_session_with_no_interruption_never_offers(self, seeded):
        """The offer belongs to a paused span. With no interrupt there is no
        span, and offering office hours would be a non-sequitur."""
        status = await escalation_status(seeded["session_id"])

        assert status.should_offer is False
        assert status.tutor_turns == 0
        assert "no interruption" in status.reason

    async def test_two_tutor_turns_just_under_the_clock_does_not_offer(self, seeded):
        """Below both thresholds. Most questions resolve here, and interrupting
        them with an escalation card would be the wrong reflex."""
        db, sid = seeded["db"], seeded["session_id"]
        self._turn(db, sid, 10, "learner", intent="interrupt", minutes_ago=4)
        self._turn(db, sid, 11, "tutor", minutes_ago=3)
        self._turn(db, sid, 12, "tutor", minutes_ago=2)

        status = await escalation_status(sid)

        assert status.should_offer is False
        assert status.tutor_turns == 2
        assert 3.5 < status.minutes_since_interrupt < 5

    async def test_three_tutor_turns_offers_on_the_turn_count(self, seeded):
        """§5.1's first limb: "3+ Tutor turns in this PAUSED_FOR_QUESTION span"."""
        db, sid = seeded["db"], seeded["session_id"]
        self._turn(db, sid, 10, "learner", intent="interrupt", minutes_ago=4)
        for index in (11, 12, 13):
            self._turn(db, sid, index, "tutor", minutes_ago=3)

        status = await escalation_status(sid)

        assert status.should_offer is True
        assert status.tutor_turns == 3
        assert "3 tutor turns" in status.reason

    async def test_ten_minutes_offers_on_the_clock_alone(self, seeded):
        """§5.1's second limb, which is an OR: one long turn still qualifies."""
        db, sid = seeded["db"], seeded["session_id"]
        self._turn(db, sid, 10, "learner", intent="interrupt", minutes_ago=12)
        self._turn(db, sid, 11, "tutor", minutes_ago=11)

        status = await escalation_status(sid)

        assert status.should_offer is True
        assert status.tutor_turns == 1
        assert "minutes paused" in status.reason

    async def test_turns_before_the_interrupt_do_not_count(self, seeded):
        """The span starts at the interrupt.

        A tutorial that ran long before the learner interrupted a later lecture
        would otherwise arrive already over the threshold, and the first
        question of the lecture would be met with "take this to office hours".
        """
        db, sid = seeded["db"], seeded["session_id"]
        for index in (1, 2, 3, 4):
            self._turn(db, sid, index, "tutor", minutes_ago=30)
        self._turn(db, sid, 10, "learner", intent="interrupt", minutes_ago=2)
        self._turn(db, sid, 11, "tutor", minutes_ago=1)

        status = await escalation_status(sid)

        assert status.tutor_turns == 1
        assert status.should_offer is False

    async def test_a_second_interrupt_starts_a_fresh_span(self, seeded):
        """A resumed lecture that is interrupted again is a new question.

        Anchoring on the *most recent* interrupt is what makes that true; the
        earlier span's turns are spent.
        """
        db, sid = seeded["db"], seeded["session_id"]
        self._turn(db, sid, 10, "learner", intent="interrupt", minutes_ago=30)
        for index in (11, 12, 13):
            self._turn(db, sid, index, "tutor", minutes_ago=28)
        self._turn(db, sid, 20, "learner", intent="interrupt", minutes_ago=2)
        self._turn(db, sid, 21, "tutor", minutes_ago=1)

        status = await escalation_status(sid)

        assert status.tutor_turns == 1
        assert status.should_offer is False

    async def test_the_predicate_and_the_status_agree(self, seeded):
        """§5.3 names both. If they could disagree, the endpoint and the button
        would be reading different computations after all."""
        db, sid = seeded["db"], seeded["session_id"]
        self._turn(db, sid, 10, "learner", intent="interrupt", minutes_ago=15)

        assert await should_offer_escalation(sid) is True
        assert (await escalation_status(sid)).should_offer is True

    async def test_the_payload_carries_the_reason_to_the_client(self, seeded):
        """§5.1's card explains itself; "the model decided" is not an answer."""
        db, sid = seeded["db"], seeded["session_id"]
        self._turn(db, sid, 10, "learner", intent="interrupt", minutes_ago=15)

        payload = (await escalation_status(sid)).as_payload()

        assert payload["should_offer"] is True
        assert payload["reason"]
        assert isinstance(payload["minutes_since_interrupt"], float)


class TestPracticeProblemWireShape:
    """v1.0.1 §6, checklist item 8, at Tier 2.

    Tier 3 asserts the same split on the parsed ``end`` payload with a live
    Curator. This asserts it on the *bytes*, which is a different claim: a
    field can be absent from the payload the test reads and still be on the
    wire -- in a sibling key, in a debug echo, in the private half if the
    Orchestrator ever forgot to strip it. §6.1's rule is about what reaches the
    browser's network log, so the network log is what this reads.
    """

    MODEL_ANSWER = "The redex reduces to the identity combinator, and here is why."
    KEY_POINT = "capture-avoiding substitution is required"

    @pytest.fixture
    def client(self, seeded):
        from fastapi.testclient import TestClient

        from studium.agents.base import AgentOutput
        from studium.agents.schemas import PracticeProblem
        from studium.api.app import app, registry
        from tests.fixtures.runtime import FakeAgent, FakeRegistry

        problem = PracticeProblem(
            prompt="Reduce (\\x. x) (\\y. y) to normal form.",
            setup="Work in the pure untyped calculus.",
            expected_minutes=5,
            difficulty=3,
            hint_ladder=["Find the redex.", "Substitute.", "Stop at normal form."],
            model_answer=self.MODEL_ANSWER,
            expected_key_points=[self.KEY_POINT],
        )

        agents = FakeRegistry(
            curator=FakeAgent(
                "curator",
                outputs={
                    "select_practice": AgentOutput(
                        text="Here is one to try.", structured=problem
                    )
                },
            )
        )
        registry._agents = agents  # type: ignore[attr-defined]
        with TestClient(app) as c:
            yield c
        registry._by_session.clear()
        registry._agents = None  # type: ignore[attr-defined]

    def _bench(self, client, seeded):
        opened = client.post(
            "/api/session",
            json={
                "user_id": str(seeded["fixture"].user.id),
                "mode": "tutorial",
                "focus_concept_id": str(seeded["fixture"].concept_id("beta-reduction")),
                "learner_subject_id": str(seeded["fixture"].enrollment.id),
            },
        ).json()
        return client.post(
            f"/api/session/{opened['session_id']}/primitive",
            json={"primitive": "let_me_try_one"},
        )

    async def test_the_answer_key_is_not_on_the_wire(self, client, seeded):
        """§6.1: server-only means it does not leave the process."""
        body = self._bench(client, seeded).text

        assert "model_answer" not in body, (
            "the answer key's field name is in the SSE body -- §6.1 makes it "
            "server-only, and the browser's network log is exactly where §11.2 "
            "does not want it"
        )
        assert self.MODEL_ANSWER not in body
        assert self.KEY_POINT not in body
        assert "expected_key_points" not in body
        assert "problem_private" not in body, (
            "the private half reached the client: the Orchestrator strips it "
            "before the chunk leaves the process, and did not"
        )

    async def test_the_bench_still_gets_what_it_renders(self, client, seeded):
        """The withholding has to leave a usable problem behind.

        A test that only asserted absence would pass on an empty payload, which
        is a bench with nothing on it.
        """
        body = self._bench(client, seeded).text

        assert "Reduce" in body, "the prompt did not reach the bench"
        assert "Work in the pure untyped calculus." in body, "the setup was dropped"
        assert "difficulty" in body

    async def test_no_hint_is_released_before_the_learner_asks(self, client, seeded):
        """§6.2: the ladder grows on click. ``hints_shown=0`` on first render."""
        body = self._bench(client, seeded).text

        assert "Substitute." not in body
        assert "Stop at normal form." not in body

    async def test_the_end_payload_matches_the_client_model_exactly(
        self, client, seeded
    ):
        """§6.3's default: a field added to the full model is server-only until
        someone adds it to the client model on purpose."""
        from studium.agents.schemas import PracticeProblemForClient

        body = self._bench(client, seeded).text
        end = None
        for line in body.splitlines():
            if not line.startswith("data: "):
                continue
            frame = json.loads(line[len("data: ") :])
            # The frame is {kind, payload}; the problem rides in the payload.
            problem = (frame.get("payload") or {}).get("problem")
            if problem:
                end = problem

        assert end is not None, "no end chunk carried a problem"
        assert set(end) <= set(PracticeProblemForClient.model_fields), (
            f"fields on the wire that the client model does not declare: "
            f"{sorted(set(end) - set(PracticeProblemForClient.model_fields))}"
        )
