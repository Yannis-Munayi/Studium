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
from tests.fixtures.runtime import FakeRegistry


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
    orchestrator.machine.state = state

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
