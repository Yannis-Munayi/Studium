"""The HTTP surface (agent runtime §20).

SSE from FastAPI to the client, one-directional. The interrupt arrives on a
separate short POST rather than through the streaming channel, because SSE
carries server-to-client only -- §20 is explicit that WebSockets are not used.

The frontend is subsystem 4's job. §26 says a curl-driven integration test
suffices to demonstrate the runtime works, and that is what this surface is
sized for: enough to drive a session end to end, no rendering concerns.

**Orchestrators live in memory, keyed by session.** §7 makes that a deliberate
choice -- no ``current_state`` column, no synchronisation bug surface. The
registry below is therefore process-local, which is correct at MVP scale (one
node, three learners) and is the first thing to move to Redis when it is not.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text as sql

from studium.agents.orchestrator import LearnerInput, Orchestrator
from studium.agents.schemas import PRIMITIVE_NAMES
from studium.orchestration.handoff import AgentRegistry
from studium.orchestration.state_machine import reconstruct_state
from studium.orchestration.streaming import sse_stream
from studium.retrieval import CacheWarmer, default_retriever
from studium.session import memory
from studium.session.budget_gate import BudgetExceededError
from studium.session.lifecycle import NoEnrollmentError

log = logging.getLogger(__name__)

#: Retrieval §14. Off unless STUDIUM_WARM_CACHE is set, because it makes a
#: retrieval call every few minutes forever -- which is the intended behaviour
#: in a deployment and a surprise everywhere else. A test run, a migration
#: check, or a developer starting the app to look at one endpoint should not
#: quietly begin billing a reranker on a timer.
WARM_CACHE_ENV = "STUDIUM_WARM_CACHE"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the §14 cache warmer alongside the app."""
    warmer: CacheWarmer | None = None

    if os.environ.get(WARM_CACHE_ENV) == "1":
        warmer = CacheWarmer(retriever=registry.retriever())
        warmer.start()
        app.state.cache_warmer = warmer
    else:
        app.state.cache_warmer = None
        log.info("cache warming disabled; set %s=1 to enable", WARM_CACHE_ENV)

    try:
        yield
    finally:
        if warmer is not None:
            await warmer.stop()


app = FastAPI(
    title="Studium Agent Runtime",
    version="1.0.0",
    description="Subsystem 2: the agents that produce every learner-facing behaviour.",
    lifespan=lifespan,
)


class OrchestratorRegistry:
    """Process-local Orchestrators, one per active session.

    A session whose Orchestrator is missing (process restarted, or the request
    landed on a different worker) gets a fresh one with its state rebuilt from
    the ``session_turns`` tail, per §7's persistence note. The rebuild is
    coarse by design -- it recovers the mode, not a mid-sentence interruption,
    because the stream that interruption belonged to died with the process.
    """

    def __init__(self) -> None:
        self._by_session: dict[uuid.UUID, Orchestrator] = {}
        self._agents: AgentRegistry | None = None
        self._retriever: Any = None

    def retriever(self) -> Any:
        """The one retriever the agents share.

        Held here rather than left to ``AgentRegistry.build``'s default because
        the cache warmer has to warm *this* instance: the result cache lives on
        the retriever, so warming a second one would fill a cache nothing reads
        and leave every real call cold (§14).
        """
        if self._retriever is None:
            self._retriever = default_retriever()
        return self._retriever

    def agents(self) -> AgentRegistry:
        if self._agents is None:
            self._agents = AgentRegistry.build(retriever=self.retriever())
        return self._agents

    def get(self, session_id: uuid.UUID) -> Orchestrator | None:
        return self._by_session.get(session_id)

    def put(self, session_id: uuid.UUID, orchestrator: Orchestrator) -> None:
        self._by_session[session_id] = orchestrator

    async def resolve(self, session_id: uuid.UUID) -> Orchestrator:
        existing = self._by_session.get(session_id)
        if existing is not None:
            return existing

        orchestrator = Orchestrator(agents=self.agents())
        context = await memory.assemble_context(session_id)
        orchestrator.machine.state = reconstruct_state(
            context.recent_turns, mode=context.mode
        )
        orchestrator.exchange_index = _last_exchange_index(context.recent_turns)
        log.info(
            "rebuilt orchestrator for session %s in state %s",
            session_id, orchestrator.machine.state,
        )
        self._by_session[session_id] = orchestrator
        return orchestrator

    def release(self, session_id: uuid.UUID) -> None:
        self._by_session.pop(session_id, None)


registry = OrchestratorRegistry()

#: Distinguishes "this artifact has no citations" from "this artifact does not
#: exist" -- the citations query alone returns an empty list for both.
_ARTIFACT_EXISTS = sql("SELECT 1 FROM content_artifacts WHERE id = :artifact_id")


def get_registry() -> OrchestratorRegistry:
    return registry


# --- request / response models --------------------------------------------


class StartSessionRequest(BaseModel):
    user_id: uuid.UUID
    mode: str = "tutorial"
    focus_concept_id: uuid.UUID | None = None
    learner_subject_id: uuid.UUID | None = None
    target_duration_minutes: int = 90


class StartSessionResponse(BaseModel):
    session_id: uuid.UUID
    state: str


class TurnRequest(BaseModel):
    text: str = ""
    primitive: str | None = Field(
        default=None,
        description="An explicit primitive button press. Skips intent classification.",
    )


class InterruptResponse(BaseModel):
    accepted: bool
    state: str
    detail: str


class CloseRequest(BaseModel):
    reason: str = "learner_stop"


class CloseResponse(BaseModel):
    session_id: uuid.UUID
    ended: bool
    summarised: bool
    errors: list[str] = Field(default_factory=list)


# --- endpoints -------------------------------------------------------------


@app.post("/api/session", response_model=StartSessionResponse, status_code=201)
async def start_session(
    body: StartSessionRequest,
    reg: OrchestratorRegistry = Depends(get_registry),
) -> StartSessionResponse:
    """Open a session and run OPENING (§16)."""
    orchestrator = Orchestrator(agents=reg.agents())
    try:
        session_id = await orchestrator.start_session(
            body.user_id,
            body.mode,
            body.focus_concept_id,
            learner_subject_id=body.learner_subject_id,
            target_duration_minutes=body.target_duration_minutes,
        )
    except BudgetExceededError as exc:
        # 402 rather than 403: the request is well-formed and the learner is
        # authorised; they are over a spend cap, which is what 402 means.
        raise HTTPException(
            status_code=402,
            detail={
                "message": exc.learner_message(),
                "scope": exc.scope,
                "reset_at": exc.reset_at.isoformat(),
            },
        ) from exc
    except NoEnrollmentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    reg.put(session_id, orchestrator)
    return StartSessionResponse(
        session_id=session_id, state=orchestrator.machine.state.value
    )


@app.post("/api/session/{session_id}/turn")
async def take_turn(
    session_id: uuid.UUID,
    body: TurnRequest,
    request: Request,
    reg: OrchestratorRegistry = Depends(get_registry),
) -> StreamingResponse:
    """Stream one turn's response as SSE (§20)."""
    if body.primitive and body.primitive not in PRIMITIVE_NAMES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown primitive {body.primitive!r}; expected one of {list(PRIMITIVE_NAMES)}",
        )

    orchestrator = await reg.resolve(session_id)
    learner_input = LearnerInput(text=body.text, primitive=body.primitive)

    # A fresh interrupt state per turn: an interrupt signalled during the
    # previous turn must not cut this one short.
    orchestrator.interruption.clear()

    return StreamingResponse(
        sse_stream(orchestrator.handle_turn(session_id, learner_input)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Without this an nginx or CDN buffer holds the whole stream and
            # delivers it as one block, which looks exactly like the server
            # having hung (§25 forward-refs SSE proxying to Infrastructure).
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/session/{session_id}/interrupt", response_model=InterruptResponse)
async def interrupt(
    session_id: uuid.UUID,
    reg: OrchestratorRegistry = Depends(get_registry),
) -> InterruptResponse:
    """Raise a hand mid-stream (§20).

    Deliberately cheap and always 200: the client fires this the instant the
    learner gestures, and an interrupt that arrives when nothing is streaming
    is a no-op rather than an error worth showing anyone.
    """
    orchestrator = reg.get(session_id)
    if orchestrator is None:
        return InterruptResponse(
            accepted=False,
            state="unknown",
            detail="no active stream for this session on this node",
        )

    accepted = orchestrator.signal_interrupt()
    return InterruptResponse(
        accepted=accepted,
        state=orchestrator.machine.state.value,
        detail=(
            "interrupt signalled; the current sentence will finish"
            if accepted
            else f"nothing interruptible in state {orchestrator.machine.state.value}"
        ),
    )


@app.post("/api/session/{session_id}/close", response_model=CloseResponse)
async def close(
    session_id: uuid.UUID,
    body: CloseRequest,
    reg: OrchestratorRegistry = Depends(get_registry),
) -> CloseResponse:
    """Run CLOSING and release the in-memory state (§16)."""
    orchestrator = await reg.resolve(session_id)
    from studium.session.lifecycle import close_session

    context = await memory.assemble_context(session_id)
    report = await close_session(context, orchestrator.agents, reason=body.reason)
    reg.release(session_id)

    return CloseResponse(
        session_id=session_id,
        ended=report.ended,
        summarised=report.summarised,
        errors=report.errors,
    )


@app.get("/api/session/{session_id}/state")
async def session_state(
    session_id: uuid.UUID,
    reg: OrchestratorRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Current runtime state. Diagnostic surface for the curl-driven tests."""
    orchestrator = await reg.resolve(session_id)
    return {
        "session_id": str(session_id),
        "state": orchestrator.machine.state.value,
        "persisted_mode": orchestrator.machine.persisted_mode,
        "exchange_index": orchestrator.exchange_index,
        "interruptible": orchestrator.machine.is_interruptible,
        "transitions": [
            {
                "from": r.source.value,
                "event": r.event.value,
                "to": r.target.value,
                "effect": r.effect,
                "at": r.at.isoformat(),
            }
            for r in orchestrator.machine.log
        ],
    }


@app.get("/api/artifacts/{artifact_id}/citations")
async def artifact_citations(artifact_id: uuid.UUID) -> dict[str, Any]:
    """Resolve an artifact's ``[Pn]`` markers to passages (retrieval §12).

    One call per artifact, not per marker: a lecture segment carries six or
    more citations and the client renders them together, so per-marker requests
    would be six round trips to draw one paragraph.

    The numbering is reconstructed from the stored ``content_citations`` rows in
    ``chunk_id`` order -- the same order retrieval numbered them in. Nothing
    stores the passage number itself, because chunk ids are stable and passage
    numbers are per-call ephemera.

    An artifact with no citations returns an empty list rather than a 404: an
    ungrounded artifact is a real thing the client must render (as prose with
    no markers), and a 404 would make it indistinguishable from a bad id.
    """
    from studium.asyncdb import read_db
    from studium.retrieval import resolve_artifact_citations

    exists = await read_db(
        lambda s: s.execute(
            _ARTIFACT_EXISTS, {"artifact_id": artifact_id}
        ).scalar()
    )
    if not exists:
        raise HTTPException(status_code=404, detail=f"no artifact {artifact_id}")

    citations = await read_db(
        lambda s: resolve_artifact_citations(s, artifact_id)
    )
    return {
        "artifact_id": str(artifact_id),
        "citations": [c.as_json() for c in citations],
    }


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Liveness, plus whether the §14 warmer is actually running.

    Warming is the kind of background work that fails silently and shows up as
    a latency regression nobody can attribute. Reporting it here means "is the
    warmer up" is answerable without reading logs.
    """
    warmer: CacheWarmer | None = getattr(request.app.state, "cache_warmer", None)
    return {
        "status": "ok",
        "subsystem": "agent-runtime",
        "version": "1.0.0",
        "cache_warming": warmer.status() if warmer else {"running": False},
    }


def _last_exchange_index(turns: list[dict[str, Any]]) -> int:
    """Recover the exchange counter so a rebuilt Orchestrator does not restart it."""
    best = 0
    for turn in turns:
        payload = turn.get("input") or {}
        if isinstance(payload, dict):
            best = max(best, int(payload.get("exchange_index", 0) or 0))
    return best
