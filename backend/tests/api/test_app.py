"""The HTTP surface (agent runtime §20).

§26: "a curl-driven integration test suffices to demonstrate the runtime works."
These cover the parts of that surface that do not need a database -- routing,
validation, SSE framing, and the out-of-band interrupt channel -- so the shape
of the API is checked in every environment, not only where Postgres is up.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from studium.agents.base import StreamChunk
from studium.agents.orchestrator import Orchestrator
from studium.api.app import app, registry
from studium.orchestration.state_machine import State
from tests.fixtures.runtime import FakeRegistry, drive_to


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
    registry._by_session.clear()


@pytest.fixture
def session_id():
    return uuid.uuid4()


def _install(session_id: uuid.UUID, state: State = State.LECTURING) -> Orchestrator:
    """Register an orchestrator with a canned turn, bypassing the database."""
    orchestrator = Orchestrator(agents=FakeRegistry())  # type: ignore[arg-type]
    drive_to(orchestrator.machine, state)

    async def fake_turn(sid, learner_input):
        yield StreamChunk.text_chunk("Beta-reduction ")
        yield StreamChunk.text_chunk("substitutes.")
        yield StreamChunk.ended(next_state=orchestrator.machine.state.value)

    orchestrator.handle_turn = fake_turn  # type: ignore[method-assign]
    registry.put(session_id, orchestrator)
    return orchestrator


class TestHealth:
    def test_reports_the_subsystem(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["subsystem"] == "agent-runtime"


class TestTurnStreaming:
    def test_streams_sse_frames_and_terminates(self, client, session_id):
        _install(session_id)
        response = client.post(f"/api/session/{session_id}/turn", json={"text": "why?"})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = response.text
        # Three chunks plus the terminator, which also carries a data: line.
        assert body.count("data: ") == 4
        assert "Beta-reduction" in body
        assert body.rstrip().endswith("event: done\ndata: {}")

    def test_proxy_buffering_is_disabled(self, client, session_id):
        """A buffering proxy holds the whole stream and looks like a hang."""
        _install(session_id)
        response = client.post(f"/api/session/{session_id}/turn", json={"text": "hi"})
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["cache-control"] == "no-cache"

    def test_an_unknown_primitive_is_rejected_before_any_model_call(self, client, session_id):
        """422 rather than a dispatch failure deep in the turn."""
        _install(session_id)
        response = client.post(
            f"/api/session/{session_id}/turn",
            json={"text": "", "primitive": "teach_me_telepathy"},
        )
        assert response.status_code == 422
        assert "teach_me_telepathy" in response.json()["detail"]

    @pytest.mark.parametrize(
        "primitive",
        ["explain_differently", "im_lost", "let_me_try_one", "prove_it_to_me"],
    )
    def test_every_real_primitive_is_accepted(self, client, session_id, primitive):
        _install(session_id)
        response = client.post(
            f"/api/session/{session_id}/turn", json={"text": "", "primitive": primitive}
        )
        assert response.status_code == 200

    def test_a_client_may_declare_an_intent_it_already_knows(self, client, session_id):
        """§8's classifier is a model call over words. Some controls are not words.

        The bench's Submit is an answer whatever the learner typed, and a
        classifier reading "It reduces to the identity." as a comment routes it
        to the Tutor and never grades it. Declaring the intent is the same move
        the primitive field already makes, for the same reason.
        """
        captured: dict[str, object] = {}
        orchestrator = _install(session_id, State.LAB)

        async def capture(sid, learner_input):
            captured["intent"] = learner_input.intent
            captured["text"] = learner_input.text
            yield StreamChunk.ended()

        orchestrator.handle_turn = capture  # type: ignore[method-assign]

        response = client.post(
            f"/api/session/{session_id}/turn",
            json={"text": "It reduces to the identity.", "intent": "answer"},
        )

        assert response.status_code == 200
        assert captured["intent"] == "answer"

    @pytest.mark.parametrize(
        "intent",
        # The primitives have their own field, and the two out-of-band signals
        # have their own endpoints. Accepting them here would give one signal a
        # second spelling that could disagree with the first.
        ["primitive:let_me_try_one", "interrupt", "end_session", "daydream"],
    )
    def test_an_intent_the_turn_channel_does_not_accept_is_rejected(
        self, client, session_id, intent
    ):
        _install(session_id)
        response = client.post(
            f"/api/session/{session_id}/turn", json={"text": "", "intent": intent}
        )
        assert response.status_code == 422
        assert intent in response.json()["detail"]

    def test_a_turn_with_no_declared_intent_still_classifies(self, client, session_id):
        """The declaration is optional; free-form text is the ordinary path."""
        captured: dict[str, object] = {}
        orchestrator = _install(session_id)

        async def capture(sid, learner_input):
            captured["intent"] = learner_input.intent
            yield StreamChunk.ended()

        orchestrator.handle_turn = capture  # type: ignore[method-assign]

        client.post(f"/api/session/{session_id}/turn", json={"text": "why?"})
        assert captured["intent"] is None

    def test_a_stale_interrupt_does_not_cut_the_next_turn_short(self, client, session_id):
        """The interrupt state is cleared per turn (§20)."""
        orchestrator = _install(session_id)
        orchestrator.interruption.signal()

        client.post(f"/api/session/{session_id}/turn", json={"text": "next"})
        assert orchestrator.interruption.interrupted is False


class TestInterrupt:
    def test_signals_an_interruptible_state(self, client, session_id):
        orchestrator = _install(session_id, State.LECTURING)
        body = client.post(f"/api/session/{session_id}/interrupt").json()

        assert body["accepted"] is True
        assert body["state"] == "LECTURING"
        assert orchestrator.interruption.interrupted is True

    @pytest.mark.parametrize("state", [State.LAB, State.CLOSING, State.REVIEW])
    def test_a_no_op_interrupt_is_reported_not_errored(self, client, session_id, state):
        """An interrupt when nothing is streaming is not worth showing anyone."""
        _install(session_id, state)
        response = client.post(f"/api/session/{session_id}/interrupt")

        assert response.status_code == 200
        assert response.json()["accepted"] is False

    def test_an_unknown_session_returns_200_not_404(self, client):
        """The client fires this the instant the learner gestures.

        A 404 here would surface a scary error for a race the learner caused by
        interrupting a stream that had already finished.
        """
        response = client.post(f"/api/session/{uuid.uuid4()}/interrupt")
        assert response.status_code == 200
        assert response.json()["accepted"] is False


class TestStateEndpoint:
    def test_exposes_the_transition_log_for_diagnosis(self, client, session_id):
        orchestrator = _install(session_id, State.LECTURING)
        orchestrator.machine.fire(
            __import__(
                "studium.orchestration.state_machine", fromlist=["Event"]
            ).Event.SEGMENT_COMPLETE,
            __import__(
                "studium.orchestration.state_machine", fromlist=["GuardContext"]
            ).GuardContext(segments_remaining=2),
        )

        body = client.get(f"/api/session/{session_id}/state").json()
        assert body["state"] == "LECTURING"
        assert body["persisted_mode"] == "lecture"
        assert body["interruptible"] is True
        assert body["transitions"][-1]["event"] == "segment_complete"


class TestValidation:
    def test_a_malformed_session_id_is_rejected(self, client):
        assert client.post("/api/session/not-a-uuid/interrupt").status_code == 422

    def test_start_session_requires_a_user(self, client):
        assert client.post("/api/session", json={"mode": "tutorial"}).status_code == 422

    def test_a_malformed_artifact_id_is_rejected(self, client):
        assert client.get("/api/artifacts/not-a-uuid/citations").status_code == 422


class TestCitationResolution:
    """Retrieval §12's read-time endpoint, at the HTTP boundary.

    The resolution logic itself is Tier 2's (it needs real rows). What these
    check is the contract subsystem 4 will build against: the response shape,
    and that a missing artifact is distinguishable from an ungrounded one.
    """

    def test_it_returns_the_documented_envelope(self, client, monkeypatch):
        import studium.api.app as app_module
        from studium.retrieval.citations import ResolvedCitation

        chunk_id = uuid.uuid4()
        source_id = uuid.uuid4()
        artifact_id = uuid.uuid4()

        resolved = [
            ResolvedCitation(
                marker="P1",
                chunk_id=chunk_id,
                source_id=source_id,
                source_title="An Introduction to Functional Programming",
                source_authors=["Michaelson, Greg"],
                page_start=42,
                page_end=42,
                section_path=["Chapter 3", "3.2 Beta Reduction"],
                excerpt="The reduction of a beta-redex proceeds by...",
                excerpt_start_offset=0,
                excerpt_end_offset=44,
            )
        ]

        calls = {"n": 0}

        async def fake_read_db(fn):
            calls["n"] += 1
            return 1 if calls["n"] == 1 else resolved

        monkeypatch.setattr("studium.asyncdb.read_db", fake_read_db)
        monkeypatch.setattr(app_module, "_ARTIFACT_EXISTS", "unused")

        body = client.get(f"/api/artifacts/{artifact_id}/citations").json()

        assert body["artifact_id"] == str(artifact_id)
        [citation] = body["citations"]
        assert citation["marker"] == "P1"
        assert citation["chunk_id"] == str(chunk_id)
        assert citation["source_title"]
        assert citation["source_authors"] == ["Michaelson, Greg"]
        assert citation["page_start"] == 42
        assert citation["section_path"] == ["Chapter 3", "3.2 Beta Reduction"]
        assert citation["excerpt"]
        assert citation["source_deleted"] is False

    def test_a_missing_artifact_is_a_404(self, client, monkeypatch):
        """Distinguishable from an artifact that simply cites nothing, which
        returns 200 and an empty list -- an ungrounded artifact is a real
        thing the client has to render."""
        async def fake_read_db(fn):
            return None

        monkeypatch.setattr("studium.asyncdb.read_db", fake_read_db)

        response = client.get(f"/api/artifacts/{uuid.uuid4()}/citations")
        assert response.status_code == 404

    def test_an_artifact_with_no_citations_is_200_and_empty(self, client, monkeypatch):
        calls = {"n": 0}

        async def fake_read_db(fn):
            calls["n"] += 1
            return 1 if calls["n"] == 1 else []

        monkeypatch.setattr("studium.asyncdb.read_db", fake_read_db)

        response = client.get(f"/api/artifacts/{uuid.uuid4()}/citations")
        assert response.status_code == 200
        assert response.json()["citations"] == []


class TestPrimitiveEndpoint:
    """v1.0.1 §3.1's named emission path, and §3.2's validity check.

    The value of a dedicated endpoint over ``POST /turn`` with a primitive
    field is that this one can answer "is that legal here?" *before* opening a
    stream. §3.2: "Silent no-op is not acceptable."
    """

    @pytest.mark.parametrize(
        "primitive,state",
        [
            ("let_me_try_one", State.LECTURING),
            ("let_me_try_one", State.TUTORIAL),
            ("prove_it_to_me", State.OFFICE_HOURS),
            ("im_lost", State.LAB),
            ("show_worked_example", State.PAUSED_FOR_QUESTION),
            ("where_does_this_fit", State.OFFICE_HOURS),
        ],
    )
    def test_a_valid_combination_streams(self, client, session_id, primitive, state):
        _install(session_id, state)
        response = client.post(
            f"/api/session/{session_id}/primitive", json={"primitive": primitive}
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

    @pytest.mark.parametrize(
        "primitive,state",
        # Checklist item 3 asks for "at least three cross-source-state cases".
        # These are the four that matter: the bench primitive invoked from
        # states that have no bench, and the two the matrix withholds from LAB
        # and OFFICE_HOURS.
        [
            ("let_me_try_one", State.LAB),
            ("let_me_try_one", State.REVIEW),
            ("explain_differently", State.LAB),
            ("show_worked_example", State.OFFICE_HOURS),
        ],
    )
    def test_an_invalid_combination_is_refused_before_the_stream_opens(
        self, client, session_id, primitive, state
    ):
        _install(session_id, state)
        response = client.post(
            f"/api/session/{session_id}/primitive", json={"primitive": primitive}
        )

        assert response.status_code == 400
        assert not response.headers["content-type"].startswith("text/event-stream")
        detail = response.json()["detail"]
        assert detail["primitive"] == primitive
        assert detail["state"] == state.value
        # The client has to render something. A refusal without "where then?"
        # leaves the UI unable to say anything useful (§3.2).
        assert detail["valid_from"], "a refusal with no valid_from is a silent no-op"
        assert state.value not in detail["valid_from"]

    def test_an_unknown_primitive_is_422_not_400(self, client, session_id):
        """A different failure: malformed request vs. legal-but-not-here."""
        _install(session_id)
        response = client.post(
            f"/api/session/{session_id}/primitive",
            json={"primitive": "teach_telepathy"},
        )
        assert response.status_code == 422

    def test_the_refusal_names_a_state_that_would_have_worked(self, client, session_id):
        """valid_from has to be actionable, not decorative."""
        from studium.orchestration.state_machine import primitive_is_valid

        _install(session_id, State.LAB)
        detail = client.post(
            f"/api/session/{session_id}/primitive",
            json={"primitive": "let_me_try_one"},
        ).json()["detail"]

        for name in detail["valid_from"]:
            assert primitive_is_valid("let_me_try_one", State(name))


class TestPracticeSubmitEndpoint:
    """§3.1: LAB and REVIEW share ``answer_submitted``."""

    @pytest.mark.parametrize("state", [State.LAB, State.REVIEW])
    def test_a_submission_declares_the_answer_intent(self, client, session_id, state):
        """The learner pressed Submit, so there is nothing left to classify.

        Paying a Haiku call to re-derive a signal the client already sent is
        the waste §8 avoids for primitives, for the same reason.
        """
        captured: dict[str, object] = {}
        orchestrator = _install(session_id, state)

        async def capture(sid, learner_input):
            captured["intent"] = learner_input.intent
            captured["text"] = learner_input.text
            yield StreamChunk.ended()

        orchestrator.handle_turn = capture  # type: ignore[method-assign]

        response = client.post(
            f"/api/session/{session_id}/practice/submit",
            json={"answer": "It reduces to the identity."},
        )

        assert response.status_code == 200
        assert captured["intent"] == "answer"
        assert captured["text"] == "It reduces to the identity."


class TestResumeEndpoint:
    """§5.2: the resumption card's wire path."""

    def test_resuming_a_paused_lecture_returns_the_new_state(self, client, session_id):
        _install(session_id, State.PAUSED_FOR_QUESTION)
        response = client.post(f"/api/session/{session_id}/resume", json={})

        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "LECTURING"
        assert body["session_id"] == str(session_id)

    def test_it_returns_json_not_a_stream(self, client, session_id):
        """§5.2: this endpoint's job is the transition; the recap arrives on
        the next turn's stream. Conflating them would leave the client unable
        to tell "resume accepted" from "recap still generating"."""
        _install(session_id, State.PAUSED_FOR_QUESTION)
        response = client.post(f"/api/session/{session_id}/resume", json={})
        assert response.headers["content-type"].startswith("application/json")

    @pytest.mark.parametrize("state", [State.LECTURING, State.LAB, State.INTERRUPTED])
    def test_resuming_from_anywhere_else_is_a_409(self, client, session_id, state):
        _install(session_id, state)
        response = client.post(f"/api/session/{session_id}/resume", json={})

        assert response.status_code == 409
        assert response.json()["detail"]["state"] == state.value

    def test_the_machine_does_not_move_on_a_refused_resume(self, client, session_id):
        """A 409 that transitioned anyway would be worse than a 200."""
        orchestrator = _install(session_id, State.LECTURING)
        client.post(f"/api/session/{session_id}/resume", json={})
        assert orchestrator.machine.state is State.LECTURING


class TestEscalateEndpoint:
    """§5.2: office hours, and the server-side re-check of the threshold."""

    @pytest.fixture
    def threshold_met(self, monkeypatch):
        import studium.api.app as app_module
        from studium.session.escalation import EscalationStatus

        async def met(session_id, *, now=None):
            return EscalationStatus(
                should_offer=True,
                tutor_turns=4,
                minutes_since_interrupt=12.0,
                reason="4 tutor turns over 12.0 minutes",
            )

        monkeypatch.setattr(app_module, "escalation_status", met)

    @pytest.fixture
    def threshold_unmet(self, monkeypatch):
        import studium.api.app as app_module
        from studium.session.escalation import EscalationStatus

        async def unmet(session_id, *, now=None):
            return EscalationStatus(
                should_offer=False,
                tutor_turns=1,
                minutes_since_interrupt=2.0,
                reason="1 tutor turn over 2.0 minutes",
            )

        monkeypatch.setattr(app_module, "escalation_status", unmet)

    def test_escalating_a_qualified_pause_reaches_office_hours(
        self, client, session_id, threshold_met, monkeypatch
    ):
        import studium.api.app as app_module

        wrote: dict[str, object] = {}

        async def fake_run_db(fn):
            wrote["called"] = True
            return None

        monkeypatch.setattr(app_module, "run_db", fake_run_db)

        _install(session_id, State.PAUSED_FOR_QUESTION)
        response = client.post(f"/api/session/{session_id}/escalate", json={})

        assert response.status_code == 200
        assert response.json()["state"] == "OFFICE_HOURS"
        # §5.2: the persisted mode moves too, or a rebuilt Orchestrator (§7)
        # would restore the session as whatever it was before.
        assert wrote.get("called") is True

    def test_an_unmet_threshold_is_a_409_that_says_why(
        self, client, session_id, threshold_unmet
    ):
        """The card is time-based: one rendered at nine minutes is still on
        screen at eleven, so the server decides which side the press landed."""
        _install(session_id, State.PAUSED_FOR_QUESTION)
        response = client.post(f"/api/session/{session_id}/escalate", json={})

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["tutor_turns"] == 1
        assert "1 tutor turn" in detail["reason"]

    def test_a_refused_escalation_leaves_the_state_alone(
        self, client, session_id, threshold_unmet
    ):
        orchestrator = _install(session_id, State.PAUSED_FOR_QUESTION)
        client.post(f"/api/session/{session_id}/escalate", json={})
        assert orchestrator.machine.state is State.PAUSED_FOR_QUESTION

    @pytest.mark.parametrize("state", [State.LECTURING, State.TUTORIAL, State.LAB])
    def test_escalating_from_anywhere_else_is_a_409(
        self, client, session_id, state, threshold_met
    ):
        _install(session_id, state)
        response = client.post(f"/api/session/{session_id}/escalate", json={})
        assert response.status_code == 409
